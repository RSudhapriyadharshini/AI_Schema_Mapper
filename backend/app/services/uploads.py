"""Access to stored upload data: raw records are streamed from SQLite in ranges, never loaded whole."""
import json

from . import database as db
from .parser import flatten_record
from .sources import FieldIndexBuilder


def fetch_records(up: dict, start: int, end: int) -> list[tuple[int, dict]]:
    """Raw records idx in [start, end). Falls back to the legacy single-column format of older uploads."""
    with db.conn() as c:
        rows = c.execute("SELECT idx, raw_json FROM upload_records WHERE upload_id=? AND idx>=? AND idx<? ORDER BY idx",
                         (up["id"], start, end)).fetchall()
    if rows:
        return [(r[0], json.loads(r[1])) for r in rows]
    legacy = json.loads(up.get("records_json") or "[]")
    return [(i, legacy[i]) for i in range(start, min(end, len(legacy)))]


def load_flat(up: dict, idx: int) -> dict:
    recs = fetch_records(up, idx, idx + 1)
    return flatten_record(recs[0][1])[0] if recs else {}


def load_field_index(up: dict, batch: int = 2000) -> dict:
    """path -> {count, samples, first_raw, examples}, from the summary stored at ingest (or rebuilt for legacy uploads)."""
    with db.conn() as c:
        rows = db.rows(c, "SELECT path, records, samples_json, first_raw, examples_json FROM upload_fields WHERE upload_id=?", (up["id"],))
    if rows:
        return {r["path"]: {"count": r["records"], "samples": json.loads(r["samples_json"]), "first_raw": r["first_raw"],
                            "examples": json.loads(r["examples_json"])} for r in rows}
    b = FieldIndexBuilder()
    for start in range(0, up["num_records"], batch):
        for i, raw in fetch_records(up, start, start + batch):
            b.add(i, flatten_record(raw)[0])
    return b.idx
