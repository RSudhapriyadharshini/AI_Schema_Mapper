import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..services import database as db
from ..services import pipeline, sources
from ..services.coverage import build_canonical, schema_index
from ..services.errors import MappingError
from ..services.parser import flatten_record
from ..services.validator import coerce_value, usable

router = APIRouter(prefix="/api")


class RunRequest(BaseModel):
    upload_id: int
    use_approved_examples: bool = True
    mapping_mode: str = "per_field"  # per_field | per_field_relearn | per_source | per_source_relearn | per_record


@router.post("/runs")
def create_run(req: RunRequest):
    if not config.api_key_configured():
        raise HTTPException(400, {"code": "MISSING_API_KEY", "message": "ANTHROPIC_API_KEY is not set. Add it to .env and restart the backend."})
    try:
        return {"run_id": pipeline.start_run(req.upload_id, req.use_approved_examples, req.mapping_mode)}
    except MappingError as e:
        raise HTTPException(404 if e.code == "NOT_FOUND" else 400, {"code": e.code, "message": e.message})


def run_view(r: dict) -> dict:
    r.pop("schema_json", None)
    r["label"] = db.run_label(r["id"])
    r["thresholds"] = json.loads(r.pop("thresholds_json") or "null") or db.get_settings()
    return r


@router.get("/runs")
def list_runs():
    with db.conn() as c:
        return [run_view(r) for r in db.rows(c, db.RUN_SELECT + " ORDER BY r.id DESC")]


@router.get("/runs/{run_id}/status")
def run_status(run_id: int):
    with db.conn() as c:
        run = db.one(c, db.RUN_SELECT + " WHERE r.id=?", (run_id,))
        if not run:
            raise HTTPException(404, "Run not found.")
        steps = db.rows(c, "SELECT step_key, label, status, detail, started_at, finished_at FROM run_steps WHERE run_id=? ORDER BY position", (run_id,))
    return {"run": run_view(run), "steps": steps}


# ---- manual review ---------------------------------------------------------

class Review(BaseModel):
    action: str  # approve | reject | skip
    target_field: str | None = None


def run_schema(run: dict) -> dict:
    if not run.get("schema_json"):
        raise HTTPException(409, {"code": "OLD_SCHEMA", "message": "This run predates schema snapshots and cannot be shown or reviewed. Start a new run."})
    return json.loads(run["schema_json"])


def rebuild_canonical(c, run_id: int, record_id: int) -> None:
    run = db.one(c, "SELECT * FROM mapping_runs WHERE id=?", (run_id,))
    schema = run_schema(run)
    rws = db.rows(c, "SELECT * FROM field_mappings WHERE mapping_run_id=? AND record_id=?", (run_id, record_id))
    for r in rws:
        r["etl_can_populate"] = bool(r["etl_can_populate"])
    cj = build_canonical(rws, schema, {"run_id": run_id, "model": run["model"], "schema_version": run["schema_version"], "prompt_version": run["prompt_version"]})
    c.execute("UPDATE canonical_records SET canonical_json=? WHERE mapping_run_id=? AND record_id=?",
              (json.dumps(cj, ensure_ascii=False), run_id, record_id))


@router.post("/mappings/{mapping_id}/review")
def review(mapping_id: int, body: Review):
    if body.action not in ("approve", "reject", "skip"):
        raise HTTPException(400, "action must be approve, reject or skip")
    with db.conn() as c:
        m = db.one(c, "SELECT * FROM field_mappings WHERE id=?", (mapping_id,))
        if not m:
            raise HTTPException(404, "Mapping not found.")
        run = db.one(c, "SELECT source_name, schema_json FROM mapping_runs WHERE id=?", (m["mapping_run_id"],))
        schema = schema_index(run_schema(run))
        if body.action == "skip":
            c.execute("UPDATE field_mappings SET review_state='skipped' WHERE id=?", (mapping_id,))
            return {"ok": True}
        raw = json.loads(db.one(c, "SELECT raw_json FROM source_records WHERE id=?", (m["record_id"],))["raw_json"])
        sig, new_target = sources.signature(flatten_record(raw)[0]), None
        if body.action == "reject":
            c.execute("UPDATE field_mappings SET status='unmapped', target_field=NULL, target_value=NULL, owner=NULL, etl_can_populate=0, review_state='rejected' WHERE id=?", (mapping_id,))
        else:
            tf = body.target_field or m["target_field"]
            if tf not in schema:
                raise HTTPException(400, {"code": "INVALID_CANONICAL_FIELD", "message": f"'{tf}' is not a canonical field."})
            value = m["target_value"] if (tf == m["target_field"] and usable(m["target_value"])) else m["source_value"]
            try:
                coerce_value(value, schema[tf]["data_type"])
            except ValueError as e:
                raise HTTPException(400, {"code": "TYPE_VALIDATION", "message": f"Value not valid for {tf}: {e}"})
            c.execute("""UPDATE field_mappings SET status='mapped', target_field=?, target_value=?, owner=?, etl_can_populate=?,
                         review_state='approved' WHERE id=?""",
                      (tf, str(value).strip(), schema[tf]["owner"], int("crawler" in schema[tf]["source_priority"]), mapping_id))
            new_target = tf
            c.execute("INSERT OR REPLACE INTO approved_mappings(source_field, target_field, example_value, created_at) VALUES (?,?,?,?)",
                      (m["source_field"], tf, m["source_value"], db.now()))
        rebuild_canonical(c, m["mapping_run_id"], m["record_id"])
        db.refresh_run_counts(c, m["mapping_run_id"])
    # carry the decision into the saved source mapping so later runs reuse it
    sources.update_after_review(sig, m["source_field"], m["target_field"], body.action, new_target, m["confidence"] or 0.0, schema)
    sources.update_field_decision_after_review(run["source_name"], m["source_field"], m["target_field"], body.action, new_target, m["confidence"] or 0.0, schema)
    return {"ok": True}


# ---- saved source mappings ---------------------------------------------------

@router.get("/source-mappings")
def source_mappings():
    return sources.list_saved()


@router.get("/field-decisions")
def field_decisions():
    return sources.list_field_decisions()


@router.delete("/field-decisions")
def delete_field_decisions(source_name: str):
    sources.delete_field_decisions(source_name)
    return {"ok": True}


@router.delete("/source-mappings/{source_id}")
def delete_source_mapping(source_id: int):
    sources.delete_saved(source_id)
    return {"ok": True}


# ---- approved examples (mapping memory) -----------------------------------

@router.get("/approved-examples")
def approved_examples():
    with db.conn() as c:
        return db.rows(c, "SELECT * FROM approved_mappings ORDER BY id DESC")


@router.post("/approved-examples/seed")
def seed_examples():
    seed = json.loads(config.APPROVED_SEED_PATH.read_text())
    with db.conn() as c:
        for s in seed:
            c.execute("INSERT OR IGNORE INTO approved_mappings(source_field, target_field, example_value, created_at) VALUES (?,?,?,?)",
                      (s["source_field"], s["target_field"], s.get("example_value"), db.now()))
    return approved_examples()


@router.delete("/approved-examples/{example_id}")
def delete_example(example_id: int):
    with db.conn() as c:
        c.execute("DELETE FROM approved_mappings WHERE id=?", (example_id,))
    return {"ok": True}
