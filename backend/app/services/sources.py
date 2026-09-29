"""Saved source mappings: Claude decides once per source; those decisions are reused for later records.

A "source" is identified by its field-name signature (the sorted set of field names). Records that share
a signature came from the same template, so the *meaning* of each field is decided once and reused.
Nothing here decides what a field means: it only stores and replays Claude's (or a reviewer's) decisions.
"""
import hashlib
import json

from . import database as db
from .validator import demote, finalize_mapped_row, source_value_str, usable

CHUNK = 20  # records per normalization request


def signature(record: dict) -> str:
    return hashlib.sha256(json.dumps(sorted(record)).encode()).hexdigest()[:16]


def group_records(records: list[dict]) -> list[tuple[str, list[int]]]:
    groups: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        groups.setdefault(signature(rec), []).append(i)
    return list(groups.items())


def get_saved(sig: str, schema_version: str, prompt_version: str) -> dict | None:
    with db.conn() as c:
        r = db.one(c, "SELECT * FROM source_mappings WHERE signature=?", (sig,))
    if not r or r["schema_version"] != schema_version or r["prompt_version"] != prompt_version:
        return None
    r["decisions"] = json.loads(r.pop("decisions_json"))
    r["fields"] = json.loads(r.pop("fields_json"))
    return r


def save(sig: str, fields: list[str], decisions: list[dict], model: str, schema_version: str, prompt_version: str, run_id: int) -> None:
    ts = db.now()
    with db.conn() as c:
        c.execute(
            """INSERT INTO source_mappings(signature, fields_json, decisions_json, model, schema_version, prompt_version,
               created_run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(signature) DO UPDATE SET fields_json=excluded.fields_json, decisions_json=excluded.decisions_json,
               model=excluded.model, schema_version=excluded.schema_version, prompt_version=excluded.prompt_version,
               created_run_id=excluded.created_run_id, updated_at=excluded.updated_at, times_reused=0""",
            (sig, json.dumps(sorted(fields)), json.dumps(decisions), model, schema_version, prompt_version, run_id, ts, ts))


def mark_used(sig: str) -> None:
    with db.conn() as c:
        c.execute("UPDATE source_mappings SET times_reused = times_reused + 1 WHERE signature=?", (sig,))


def list_saved() -> list[dict]:
    with db.conn() as c:
        rows = db.rows(c, "SELECT * FROM source_mappings ORDER BY updated_at DESC")
    for r in rows:
        r["fields"] = json.loads(r.pop("fields_json"))
        r["decisions"] = json.loads(r.pop("decisions_json"))
    return rows


def delete_saved(source_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM source_mappings WHERE id=?", (source_id,))


def decisions_from_rows(rows: list[dict]) -> list[dict]:
    """Turn one record's validated Claude rows into reusable per-field decisions."""
    out = []
    for r in rows:
        mapped = r["llm_status"] == "mapped"
        tv = r.get("llm_target_value")
        out.append({
            "source_field": r["source_field"], "target_field": r["target_field"], "status": r["llm_status"],
            "confidence": r["confidence"], "reason": r["reason"], "owner": r["owner"],
            "etl_can_populate": bool(r.get("llm_etl_can_populate")),
            # copy = the value passes through unchanged; derived = Claude had to transform it
            "derived": bool(mapped and usable(tv) and str(tv).strip() != str(r["source_value"]).strip()),
            "example_source_value": r["source_value"], "example_target_value": tv if mapped else None,
        })
    return out


def derived_decisions(decisions: list[dict]) -> list[dict]:
    return [d for d in decisions if d["status"] == "mapped" and d["derived"]]


def rows_from_decisions(decisions: list[dict], record: dict, schema_fields: dict, thresholds: dict, derived_values: dict) -> list[dict]:
    """Apply saved decisions to another record. derived_values: (source_field, target_field) -> value from Claude."""
    rows = []
    for d in decisions:
        sf = d["source_field"]
        if sf not in record:
            continue
        raw = source_value_str(record[sf])
        status = d["status"]
        row = {
            "source_field": sf, "source_value": raw, "target_field": d["target_field"], "target_value": None,
            "confidence": d["confidence"], "status": status, "llm_status": status, "reason": d["reason"],
            "owner": d["owner"], "etl_can_populate": bool(d["etl_can_populate"]) and status == "mapped",
            "validation_note": None, "origin": "reused",
            "review_state": "approved" if d.get("human_approved") else None,
        }
        if status == "mapped":
            value = derived_values.get((sf, d["target_field"])) if d["derived"] else raw.strip()
            if not usable(value):
                demote(row, "a value could not be derived for this record from the saved mapping")
            else:
                row["target_value"] = str(value).strip()
                finalize_mapped_row(row, schema_fields, thresholds, human_approved=bool(d.get("human_approved")))
        rows.append(row)
    return rows


def update_after_review(sig: str, source_field: str, old_target: str | None, action: str, new_target: str | None,
                        confidence: float, schema_fields: dict) -> None:
    """Carry a human review decision into the saved source mapping so later runs reuse it."""
    with db.conn() as c:
        r = db.one(c, "SELECT id, decisions_json FROM source_mappings WHERE signature=?", (sig,))
        if not r:
            return
        ds = json.loads(r["decisions_json"])
        mine = [d for d in ds if d["source_field"] == source_field]
        rest = [d for d in ds if d["source_field"] != source_field]
        if action == "approve":
            mine = [d for d in mine if d["status"] == "mapped" and d["target_field"] != new_target]
            mine.append({"source_field": source_field, "target_field": new_target, "status": "mapped", "confidence": confidence,
                         "reason": "Approved by a human reviewer.", "owner": schema_fields[new_target]["owner"],
                         "etl_can_populate": "crawler" in schema_fields[new_target]["source_priority"], "derived": False, "human_approved": True,
                         "example_source_value": None, "example_target_value": None})
        else:  # reject: drop the rejected entry; if nothing is left, the field is unmapped
            mine = [d for d in mine if d["target_field"] != old_target]
            if not mine:
                mine = [{"source_field": source_field, "target_field": None, "status": "unmapped", "confidence": confidence,
                         "reason": "Rejected by a human reviewer.", "owner": None, "etl_can_populate": False, "derived": False,
                         "example_source_value": None, "example_target_value": None}]
        c.execute("UPDATE source_mappings SET decisions_json=?, updated_at=? WHERE id=?", (json.dumps(rest + mine), db.now(), r["id"]))
