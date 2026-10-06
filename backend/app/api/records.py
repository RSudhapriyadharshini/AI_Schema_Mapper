import json

from fastapi import APIRouter, HTTPException

from ..services import database as db
from ..services.coverage import coverage_from_db
from .mapping import run_schema, run_view

router = APIRouter(prefix="/api")


def _run(c, run_id: int) -> dict:
    run = db.one(c, db.RUN_SELECT + " WHERE r.id=?", (run_id,))
    if not run:
        raise HTTPException(404, "Run not found.")
    return run_view(run)


def _band(rows: list[dict], thresholds: dict) -> list[dict]:
    for r in rows:
        r["etl_can_populate"] = bool(r["etl_can_populate"])
        r["band"] = "Approved" if r["review_state"] == "approved" else db.band(r["confidence"], thresholds)
    return rows


@router.get("/runs/{run_id}/mappings")
def mappings(run_id: int, record: int | None = None, status: str | None = None, limit: int = 300, offset: int = 0):
    """A page of mapping rows. Runs can hold millions of rows, so this never returns them all."""
    limit = min(max(limit, 1), 1000)
    where, params = "m.mapping_run_id=?", [run_id]
    if record is not None:
        where += " AND s.record_index=?"
        params.append(record)
    if status:
        where += " AND m.status=?"
        params.append(status)
    with db.conn() as c:
        run = _run(c, run_id)
        total = c.execute(f"SELECT COUNT(*) FROM field_mappings m JOIN source_records s ON s.id=m.record_id WHERE {where}", params).fetchone()[0]
        rows = db.rows(c, f"""SELECT m.*, s.record_index FROM field_mappings m JOIN source_records s ON s.id=m.record_id
                              WHERE {where} ORDER BY s.record_index, m.id LIMIT ? OFFSET ?""", (*params, limit, max(offset, 0)))
    return {"run": run, "mappings": _band(rows, run["thresholds"]), "total": total, "limit": limit, "offset": max(offset, 0)}


@router.get("/runs/{run_id}/review-queue")
def review_queue(run_id: int, include_unmapped: bool = False, limit: int = 50):
    """One representative row per distinct decision awaiting review, biggest impact first."""
    statuses = ("ambiguous", "unmapped") if include_unmapped else ("ambiguous",)
    marks = ",".join("?" * len(statuses))
    open_sql = f"mapping_run_id=? AND status IN ({marks}) AND COALESCE(review_state,'') NOT IN ('skipped','rejected','approved')"
    limit = min(max(limit, 1), 200)
    with db.conn() as c:
        run = _run(c, run_id)
        groups = c.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM field_mappings WHERE {open_sql} GROUP BY source_field, status, COALESCE(target_field,''))", (run_id, *statuses)).fetchone()[0]
        rows = db.rows(c, f"""SELECT m.*, s.record_index, g.n AS affects FROM field_mappings m
                              JOIN source_records s ON s.id=m.record_id
                              JOIN (SELECT MIN(id) AS mid, COUNT(*) AS n FROM field_mappings WHERE {open_sql}
                                    GROUP BY source_field, status, COALESCE(target_field,'')) g ON g.mid=m.id
                              ORDER BY g.n DESC, m.id LIMIT ?""", (run_id, *statuses, limit))
    return {"run": run, "items": _band(rows, run["thresholds"]), "total_decisions": groups}


@router.get("/runs/{run_id}/canonical")
def canonical(run_id: int, limit: int = 30, offset: int = 0):
    limit = min(max(limit, 1), 200)
    with db.conn() as c:
        run = _run(c, run_id)
        total = c.execute("SELECT COUNT(*) FROM canonical_records WHERE mapping_run_id=?", (run_id,)).fetchone()[0]
        recs = db.rows(c, """SELECT c.id, c.record_id, c.canonical_json, s.record_index,
                                    COALESCE(NULLIF(s.raw_json, ''), (SELECT raw_json FROM upload_records u
                                                                      WHERE u.upload_id=? AND u.idx=s.record_index)) AS raw_json
                             FROM canonical_records c JOIN source_records s ON s.id=c.record_id
                             WHERE c.mapping_run_id=? ORDER BY s.record_index LIMIT ? OFFSET ?""",
                       (run["upload_id"], run_id, limit, max(offset, 0)))
    for r in recs:
        r.update(json.loads(r.pop("canonical_json")))
        r["raw"] = json.loads(r.pop("raw_json") or "{}")
    return {"run": run, "records": recs, "total": total, "limit": limit, "offset": max(offset, 0)}


@router.get("/runs/{run_id}/coverage")
def coverage(run_id: int):
    with db.conn() as c:
        run = _run(c, run_id)
        if run["status"] != "completed":
            raise HTTPException(409, "Run has not completed.")
        stored = db.one(c, "SELECT schema_json, coverage_json FROM mapping_runs WHERE id=?", (run_id,))
        if stored["coverage_json"]:
            out = json.loads(stored["coverage_json"])
        else:  # cleared by a review: recompute once and cache it (this can take a while on very large runs)
            out = coverage_from_db(c, run_id, run_schema(stored), run["total_records"])
            c.execute("UPDATE mapping_runs SET coverage_json=? WHERE id=?", (json.dumps(out), run_id))
    out["run"] = run
    return out


@router.get("/runs/{run_id}/logs")
def logs(run_id: int, limit: int = 500):
    with db.conn() as c:
        run = _run(c, run_id)
        calls = db.rows(c, "SELECT * FROM llm_calls WHERE run_id=? ORDER BY id LIMIT ?", (run_id, min(max(limit, 1), 2000)))
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
    if table == "mapping_runs":
        for r in data:
            r.pop("schema_json", None)
    return {"table": table, "counts": counts, "rows": data}
