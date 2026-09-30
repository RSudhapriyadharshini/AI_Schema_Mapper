import json

from fastapi import APIRouter, HTTPException

from .. import config
from ..services import database as db
from ..services.coverage import compute_coverage
from .mapping import run_schema, run_view

router = APIRouter(prefix="/api")


def _run(c, run_id: int) -> dict:
    run = db.one(c, db.RUN_SELECT + " WHERE r.id=?", (run_id,))
    if not run:
        raise HTTPException(404, "Run not found.")
    return run_view(run)


def _mapping_rows(c, run_id: int, thresholds: dict) -> list[dict]:
    rws = db.rows(c, """SELECT m.*, s.record_index FROM field_mappings m JOIN source_records s ON s.id = m.record_id
                        WHERE m.mapping_run_id=? ORDER BY s.record_index, m.id""", (run_id,))
    for r in rws:
        r["etl_can_populate"] = bool(r["etl_can_populate"])
        r["band"] = "Approved" if r["review_state"] == "approved" else db.band(r["confidence"], thresholds)
    return rws


@router.get("/runs/{run_id}/mappings")
def mappings(run_id: int):
    with db.conn() as c:
        run = _run(c, run_id)
        rws = _mapping_rows(c, run_id, run["thresholds"])
    return {"run": run, "mappings": rws}


@router.get("/runs/{run_id}/canonical")
def canonical(run_id: int):
    with db.conn() as c:
        run = _run(c, run_id)
        recs = db.rows(c, """SELECT c.id, c.record_id, c.canonical_json, s.record_index, s.raw_json FROM canonical_records c
                             JOIN source_records s ON s.id=c.record_id WHERE c.mapping_run_id=? ORDER BY s.record_index""", (run_id,))
    for r in recs:
        r.update(json.loads(r.pop("canonical_json")))
        r["raw"] = json.loads(r.pop("raw_json"))
    return {"run": run, "records": recs}


@router.get("/runs/{run_id}/coverage")
def coverage(run_id: int):
    with db.conn() as c:
        run = _run(c, run_id)
        schema = run_schema(db.one(c, "SELECT schema_json FROM mapping_runs WHERE id=?", (run_id,)) or {})
        rws = _mapping_rows(c, run_id, run["thresholds"])
        canon = [json.loads(r["canonical_json"]) for r in db.rows(
            c, """SELECT c.canonical_json FROM canonical_records c JOIN source_records s ON s.id=c.record_id
                  WHERE c.mapping_run_id=? ORDER BY s.record_index""", (run_id,))]
    if run["status"] != "completed":
        raise HTTPException(409, "Run has not completed.")
    out = compute_coverage(schema, rws, canon, len(canon))
    out["run"] = run
    return out


@router.get("/runs/{run_id}/logs")
def logs(run_id: int):
    with db.conn() as c:
        run = _run(c, run_id)
        calls = db.rows(c, "SELECT * FROM llm_calls WHERE run_id=? ORDER BY id", (run_id,))
    return {"run": run, "calls": calls}


@router.get("/db/{table}")
def table_rows(table: str, run_id: int | None = None, limit: int = 500):
    if table not in db.TABLES:
        raise HTTPException(404, "Unknown table.")
    col = "id" if table == "mapping_runs" else "mapping_run_id"
    with db.conn() as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in db.TABLES}
        if run_id is None:
            data = db.rows(c, f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))
        else:
            data = db.rows(c, f"SELECT * FROM {table} WHERE {col}=? ORDER BY id LIMIT ?", (run_id, limit))
    return {"table": table, "counts": counts, "rows": data}
