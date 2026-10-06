"""Saved source mappings: Claude decides once per source; those decisions are reused for later records.

A "source" is identified by its field-name signature (the sorted set of field names). Records that share
a signature came from the same template, so the *meaning* of each field is decided once and reused.
Nothing here decides what a field means: it only stores and replays Claude's (or a reviewer's) decisions.
"""
import hashlib
import json

from . import database as db
from .recipes import RecipeError, result_text, run_recipe
from .validator import coerce_value, demote, finalize_mapped_row, is_copy, source_value_str, usable

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


def _recipe_reproduces(recipe: list[dict], raw: str, target_value, field: dict) -> bool:
    """A recipe is trusted only if it turns the sample source value into the same typed value Claude produced."""
    try:
        got = coerce_value(result_text(run_recipe(recipe, raw)), field["data_type"], field.get("item_fields"))
        want = coerce_value(target_value, field["data_type"], field.get("item_fields"))
    except (RecipeError, ValueError):
        return False
    return got == want


def decisions_from_rows(rows: list[dict], schema_fields: dict) -> list[dict]:
    """Turn one record's (or one field's) validated Claude rows into reusable decisions."""
    out = []
    for r in rows:
        mapped = r["llm_status"] == "mapped"
        tv = r.get("llm_target_value")
        fld = schema_fields[r["target_field"]] if mapped and r["target_field"] else None
        derived = bool(mapped and usable(tv) and not is_copy(r["source_value"], tv, fld))
        recipe = r.get("llm_recipe") if derived else None
        verified = bool(recipe and _recipe_reproduces(recipe, r["source_value"], tv, fld))
        out.append({
            "source_field": r["source_field"], "target_field": r["target_field"], "status": r["llm_status"],
            "confidence": r["confidence"], "reason": r["reason"], "owner": r["owner"],
            "etl_can_populate": bool(r.get("llm_etl_can_populate")),
            # copy = the value passes through unchanged; derived = it must be transformed, by a verified recipe or by Claude
            "derived": derived, "recipe": recipe, "recipe_verified": verified,
            "example_source_value": r["source_value"], "example_target_value": tv if mapped else None,
        })
    return out


def llm_value_decisions(decisions: list[dict]) -> list[dict]:
    """Decisions whose values still need Claude at apply time: transformed, and no verified recipe."""
    return [d for d in decisions if d["status"] == "mapped" and d["derived"] and not d.get("recipe_verified")]


def decision_kinds(decisions: list[dict]) -> dict:
    """How mapped decisions will be applied: plain copy, verified recipe, or Claude."""
    mapped = [d for d in decisions if d["status"] == "mapped"]
    recipe = sum(1 for d in mapped if d["derived"] and d.get("recipe_verified"))
    llm = sum(1 for d in mapped if d["derived"] and not d.get("recipe_verified"))
    return {"copy": len(mapped) - recipe - llm, "recipe": recipe, "llm": llm}


def rows_from_decisions(decisions: list[dict], record: dict, schema_fields: dict, thresholds: dict, derived_values: dict,
                        strict: bool = False) -> list[dict]:
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
            note = "a value could not be derived for this record from the saved mapping"
            if not d["derived"]:
                value = raw.strip()
            elif d.get("recipe_verified"):
                try:
                    value = result_text(run_recipe(d["recipe"], raw))
                except RecipeError as e:
                    value, note = None, f"the saved recipe could not be applied to this value ({e})"
            elif strict:
                value, note = None, "needs Claude to transform this value and no verified recipe exists (no-LLM run)"
            else:
                value = derived_values.get((sf, d["target_field"]))
            if not usable(value):
                demote(row, note)
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
        ds = _apply_review(json.loads(r["decisions_json"]), source_field, old_target, action, new_target, confidence, schema_fields)
        c.execute("UPDATE source_mappings SET decisions_json=?, updated_at=? WHERE id=?", (json.dumps(ds), db.now(), r["id"]))


# ---- per-field decisions ------------------------------------------------------
# In per_field mode Claude decides once per unique source field path (across all records of a source),
# and each decision is saved under the source's name so later crawls of that source can reuse it.

class FieldIndexBuilder:
    """Bounded summary of a source: per distinct field path, a record count, a few distinct sample values and
    the ids of the records they came from. Memory depends on the number of distinct fields, not records."""

    def __init__(self, max_samples: int = 5, max_chars: int = 1500):
        self.idx: dict = {}
        self.max_samples, self.max_chars = max_samples, max_chars

    def add(self, i: int, flat: dict) -> None:
        for path, v in flat.items():
            e = self.idx.get(path)
            if e is None:
                e = self.idx[path] = {"count": 0, "samples": [], "first_raw": None, "examples": []}
            e["count"] += 1
            raw = source_value_str(v)
            if e["first_raw"] is None:
                e["first_raw"] = raw
            sample = raw[:self.max_chars]
            if len(e["samples"]) < self.max_samples and sample not in e["samples"]:
                e["samples"].append(sample)
                e["examples"].append(i)


def pick_context(paths: list[str], index: dict, load_flat, k: int = 2, max_chars: int = 300) -> list[dict]:
    """Pick up to k whole records covering the most of `paths`, so Claude can see neighbouring fields.
    Candidates are only the few example records remembered per field; load_flat(i) fetches one flattened record."""
    cand_ids = sorted({i for p in paths for i in index[p]["examples"]})[:300]
    flats = {i: load_flat(i) for i in cand_ids}
    wanted = set(paths)
    covered: set = set()
    chosen: list[int] = []
    for _ in range(k):
        best = max((i for i in cand_ids if i not in chosen), key=lambda i: len((set(flats[i]) & wanted) - covered), default=None)
        if best is None:
            break
        chosen.append(best)
        covered |= set(flats[best]) & wanted
    return [{"record_index": i, "record": {p: source_value_str(v)[:max_chars] for p, v in flats[i].items()}} for i in chosen]


def get_field_decisions(source_name: str, schema_version: str, prompt_version: str | None = None) -> dict:
    """path -> decisions, for saved decisions of this source that match the current schema version.

    With a prompt_version, only decisions made with that prompt count (a normal run re-learns after a prompt change).
    With None, decisions made under any prompt version count: a no-LLM run reuses what the source already has."""
    sql, params = "SELECT * FROM field_decisions WHERE source_name=? AND schema_version=?", [source_name, schema_version]
    if prompt_version is not None:
        sql += " AND prompt_version=?"
        params.append(prompt_version)
    with db.conn() as c:
        rows = db.rows(c, sql, params)
    return {r["path"]: {"decisions": json.loads(r["decisions_json"]), "prompt_version": r["prompt_version"]} for r in rows}


def saved_sources(schema_version: str) -> list[dict]:
    with db.conn() as c:
        return db.rows(c, "SELECT source_name, COUNT(*) AS fields, GROUP_CONCAT(DISTINCT prompt_version) AS prompt_versions "
                          "FROM field_decisions WHERE schema_version=? GROUP BY source_name ORDER BY source_name", (schema_version,))


def save_field_decisions(source_name: str, by_path: dict, model: str, schema_version: str, prompt_version: str, run_id: int) -> None:
    ts = db.now()
    with db.conn() as c:
        for path, ds in by_path.items():
            c.execute(
                """INSERT INTO field_decisions(source_name, path, decisions_json, model, schema_version, prompt_version,
                   created_run_id, updated_at) VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_name, path) DO UPDATE SET decisions_json=excluded.decisions_json, model=excluded.model,
                   schema_version=excluded.schema_version, prompt_version=excluded.prompt_version,
                   created_run_id=excluded.created_run_id, updated_at=excluded.updated_at, times_reused=0""",
                (source_name, path, json.dumps(ds), model, schema_version, prompt_version, run_id, ts))


def mark_fields_used(source_name: str, paths: list[str]) -> None:
    with db.conn() as c:
        c.executemany("UPDATE field_decisions SET times_reused = times_reused + 1 WHERE source_name=? AND path=?",
                      [(source_name, p) for p in paths])


def list_field_decisions() -> list[dict]:
    with db.conn() as c:
        rows = db.rows(c, "SELECT * FROM field_decisions ORDER BY source_name, path")
    for r in rows:
        r["decisions"] = json.loads(r.pop("decisions_json"))
    return rows


def delete_field_decisions(source_name: str) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM field_decisions WHERE source_name=?", (source_name,))


def _apply_review(ds: list[dict], source_field: str, old_target, action: str, new_target, confidence: float, schema_fields: dict) -> list[dict]:
    mine = [d for d in ds if d["source_field"] == source_field]
    rest = [d for d in ds if d["source_field"] != source_field]
    if action == "approve":
        mine = [d for d in mine if d["status"] == "mapped" and d["target_field"] != new_target]
        mine.append({"source_field": source_field, "target_field": new_target, "status": "mapped", "confidence": confidence,
                     "reason": "Approved by a human reviewer.", "owner": schema_fields[new_target]["owner"],
                     "etl_can_populate": "crawler" in schema_fields[new_target]["source_priority"], "derived": False, "human_approved": True,
                     "example_source_value": None, "example_target_value": None})
    else:
        mine = [d for d in mine if d["target_field"] != old_target]
        if not mine:
            mine = [{"source_field": source_field, "target_field": None, "status": "unmapped", "confidence": confidence,
                     "reason": "Rejected by a human reviewer.", "owner": None, "etl_can_populate": False, "derived": False,
                     "example_source_value": None, "example_target_value": None}]
    return rest + mine


def update_field_decision_after_review(source_name: str, path: str, old_target, action: str, new_target, confidence: float, schema_fields: dict) -> None:
    with db.conn() as c:
        r = db.one(c, "SELECT id, decisions_json FROM field_decisions WHERE source_name=? AND path=?", (source_name, path))
        if not r:
            return
        ds = _apply_review(json.loads(r["decisions_json"]), path, old_target, action, new_target, confidence, schema_fields)
        c.execute("UPDATE field_decisions SET decisions_json=?, updated_at=? WHERE id=?", (json.dumps(ds), db.now(), r["id"]))
