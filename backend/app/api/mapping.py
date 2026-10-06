import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..services import database as db
from ..services import pipeline, sources
from ..services.coverage import build_canonical, schema_index
from ..services.errors import MappingError
from ..services.parser import flatten_record
from ..services.validator import coerce_value, to_text, usable

router = APIRouter(prefix="/api")


class RunRequest(BaseModel):
    upload_id: int
    use_approved_examples: bool = True
    mapping_mode: str = "per_field"  # per_field | per_field_relearn | per_source | per_source_relearn | per_record
    strict_no_llm: bool = False       # reuse saved decisions and recipes only; make no Claude request at all


@router.post("/runs")
def create_run(req: RunRequest):
    if not req.strict_no_llm and not config.api_key_configured():
        raise HTTPException(400, {"code": "MISSING_API_KEY", "message": "ANTHROPIC_API_KEY is not set. Add it to .env and restart the backend."})
    try:
        return {"run_id": pipeline.start_run(req.upload_id, req.use_approved_examples, req.mapping_mode, req.strict_no_llm)}
    except MappingError as e:
        raise HTTPException(404 if e.code == "NOT_FOUND" else 400, {"code": e.code, "message": e.message})


def run_view(r: dict) -> dict:
    r.pop("schema_json", None)
    r.pop("coverage_json", None)
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
    scope: str = "record"  # record: this row only | all: every row of this run with the same decision (same field, status and target)


def run_schema(run: dict) -> dict:
    if not run.get("schema_json"):
        raise HTTPException(409, {"code": "OLD_SCHEMA", "message": "This run predates schema snapshots and cannot be shown or reviewed. Start a new run."})
    return json.loads(run["schema_json"])


def rebuild_canonical(c, run_id: int, record_id: int, run: dict, schema: dict) -> None:
    rws = db.rows(c, "SELECT * FROM field_mappings WHERE mapping_run_id=? AND record_id=?", (run_id, record_id))
    for r in rws:
        r["etl_can_populate"] = bool(r["etl_can_populate"])
    cj = build_canonical(rws, schema, {"run_id": run_id, "model": run["model"], "schema_version": run["schema_version"], "prompt_version": run["prompt_version"]})
    c.execute("UPDATE canonical_records SET canonical_json=? WHERE mapping_run_id=? AND record_id=?",
              (json.dumps(cj, ensure_ascii=False), run_id, record_id))


@router.post("/mappings/{mapping_id}/review")
def review(mapping_id: int, body: Review):
    if body.action not in ("approve", "reject", "skip") or body.scope not in ("record", "all"):
        raise HTTPException(400, "action must be approve, reject or skip; scope must be record or all")
    with db.conn() as c:
        m = db.one(c, "SELECT * FROM field_mappings WHERE id=?", (mapping_id,))
        if not m:
            raise HTTPException(404, "Mapping not found.")
        run = db.one(c, "SELECT * FROM mapping_runs WHERE id=?", (m["mapping_run_id"],))
        schema = schema_index(run_schema(run))
        run_id = m["mapping_run_id"]
        if body.scope == "all":
            rows = db.rows(c, """SELECT * FROM field_mappings WHERE mapping_run_id=? AND source_field=? AND status=?
                                 AND COALESCE(target_field,'')=COALESCE(?, '') AND COALESCE(review_state,'') NOT IN ('approved','rejected')""",
                           (run_id, m["source_field"], m["status"], m["target_field"]))
        else:
            rows = [m]
        if body.action == "skip":
            c.executemany("UPDATE field_mappings SET review_state='skipped' WHERE id=?", [(r["id"],) for r in rows])
            return {"ok": True, "updated": len(rows)}
        tf = body.target_field or m["target_field"]
        if body.action == "approve":
            if tf not in schema:
                raise HTTPException(400, {"code": "INVALID_CANONICAL_FIELD", "message": f"'{tf}' is not a canonical field."})
            fld = schema[tf]
            good, bad = [], []
            for r in rows:
                value = r["target_value"] if (tf == r["target_field"] and usable(r["target_value"])) else r["source_value"]
                try:
                    good.append((to_text(coerce_value(value, fld["data_type"], fld.get("item_fields"))), r))
                except ValueError as e:
                    bad.append(str(e))
            if not good:
                raise HTTPException(400, {"code": "TYPE_VALIDATION", "message": f"Value not valid for {tf}: {bad[0]}"})
            c.executemany("""UPDATE field_mappings SET status='mapped', target_field=?, target_value=?, owner=?, etl_can_populate=?,
                             review_state='approved' WHERE id=?""",
                          [(tf, v, fld["owner"], int("crawler" in fld["source_priority"]), r["id"]) for v, r in good])
            c.execute("INSERT OR REPLACE INTO approved_mappings(source_field, target_field, example_value, created_at) VALUES (?,?,?,?)",
                      (m["source_field"], tf, m["source_value"], db.now()))
            new_target, touched = tf, [r["record_id"] for _, r in good]
        else:
            c.executemany("UPDATE field_mappings SET status='unmapped', target_field=NULL, target_value=NULL, owner=NULL, etl_can_populate=0, review_state='rejected' WHERE id=?",
                          [(r["id"],) for r in rows])
            new_target, touched = None, [r["record_id"] for r in rows]
        for rid in sorted(set(touched)):
            rebuild_canonical(c, run_id, rid, run, schema_from(run))
        db.refresh_run_counts(c, run_id)
        c.execute("UPDATE mapping_runs SET coverage_json=NULL WHERE id=?", (run_id,))
        raw = json.loads(db.one(c, "SELECT raw_json FROM source_records WHERE id=?", (m["record_id"],))["raw_json"] or "null") \
            or json.loads(db.one(c, "SELECT raw_json FROM upload_records WHERE upload_id=? AND idx=(SELECT record_index FROM source_records WHERE id=?)",
                                 (run["upload_id"], m["record_id"]))["raw_json"])
    # carry the decision into the saved decisions so later runs reuse it
    sig = sources.signature(flatten_record(raw)[0])
    sources.update_after_review(sig, m["source_field"], m["target_field"], body.action, new_target, m["confidence"] or 0.0, schema)
    sources.update_field_decision_after_review(run["source_name"], m["source_field"], m["target_field"], body.action, new_target, m["confidence"] or 0.0, schema)
    return {"ok": True, "updated": len(rows)}


def schema_from(run: dict) -> dict:
    return json.loads(run["schema_json"])


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


# ---- saved decisions -------------------------------------------------------

@router.get("/source-mappings")
def source_mappings():
    return sources.list_saved()


@router.delete("/source-mappings/{source_id}")
def delete_source_mapping(source_id: int):
    sources.delete_saved(source_id)
    return {"ok": True}


@router.get("/field-decisions")
def field_decisions():
    return sources.list_field_decisions()


@router.delete("/field-decisions")
def delete_field_decisions(source_name: str):
    sources.delete_field_decisions(source_name)
    return {"ok": True}
