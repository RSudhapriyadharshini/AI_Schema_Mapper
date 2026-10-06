"""Canonical profile construction and coverage metrics. Pure arithmetic over validated mappings."""
from collections import defaultdict

import json

from .validator import coerce_value, usable

RANK = {"mapped": 3, "ambiguous": 2, "unmapped": 1}

OWNER_ACTIONS = {
    "ETL": "Improve or extend crawler/ETL extraction for this field.",
    "PROFESSIONAL": "Request completion from the professional.",
    "OTHER_SOURCE": "Obtain from the designated regulatory / internal source.",
    "SYSTEM": "Populated by the platform (system-generated).",
}


def schema_index(schema: dict) -> dict:
    return {f["field"]: f for f in schema["fields"]}


LIST_TYPES = ("string_list", "object_list")


def nest_profile(flat: dict) -> dict:
    """Assemble the nested canonical profile: person fields at the top, company_* fields under company[0]."""
    profile: dict = {}
    company: dict = {}
    for fid, val in flat.items():
        if fid.startswith("company_social_links."):
            company.setdefault("company_social_links", {})[fid.split(".", 1)[1]] = val
        elif fid.startswith("company_"):
            company[fid] = val
        else:
            profile[fid] = val
    if company:
        profile["company"] = [company]
    return profile


def build_canonical(mapping_rows: list[dict], schema: dict, meta: dict) -> dict:
    """Build one canonical record. Scalars take the highest-confidence mapped value; list fields merge all sources.

    Returns {"profile": nested, "flat": {field_id: value}, "lineage": {field_id: {...}}}.
    `meta` = run_id, model, schema_version, prompt_version.
    """
    by_field: dict = defaultdict(list)
    for r in mapping_rows:
        if r["status"] == "mapped" and r.get("target_field") and usable(r.get("target_value")):
            by_field[r["target_field"]].append(r)
    flat, lineage = {}, {}
    for f in schema["fields"]:
        rows = by_field.get(f["field"])
        if not rows:
            continue
        typed = []
        for r in rows:
            try:
                typed.append((r, coerce_value(r["target_value"], f["data_type"], f.get("item_fields"))))
            except ValueError:
                continue
        if not typed:
            continue
        if f["data_type"] in LIST_TYPES:
            value, seen = [], set()
            for _, items in typed:
                for it in items:
                    k = json.dumps(it, sort_keys=True, ensure_ascii=False).lower()
                    if k not in seen:
                        seen.add(k)
                        value.append(it)
            srcs = sorted({r["source_field"] for r, _ in typed})
            best = typed[0][0]
            src = " + ".join(srcs)
        else:
            best, value = max(typed, key=lambda t: t[0]["confidence"] or 0)
            src = best["source_field"]
        flat[f["field"]] = value
        lineage[f["field"]] = {
            "source_field": src, "source_value": best["source_value"], "confidence": best["confidence"],
            "mapping_id": best.get("id"), "mapping_run": f"RUN-{meta['run_id']:05d}", "model": meta["model"],
            "schema_version": meta["schema_version"], "prompt_version": meta["prompt_version"],
            "review_state": best.get("review_state"),
        }
    return {"profile": nest_profile(flat), "flat": flat, "lineage": lineage}


def display_value(v) -> str | None:
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v)


def coverage_from_db(c, run_id: int, schema: dict, n_records: int) -> dict:
    """Coverage metrics computed with SQL aggregates over the stored mappings, so it works for very large runs.

    A canonical field counts as populated in a record when that record has a mapped row for it with a usable
    value (values were type-checked before they were stored).
    """
    from . import database as db

    fields = schema["fields"]
    total = len(fields)
    usable_sql = "mapping_run_id=? AND status='mapped' AND target_field IS NOT NULL AND target_value IS NOT NULL AND TRIM(target_value) <> ''"

    per = {r["target_field"]: r for r in db.rows(
        c, f"SELECT target_field, COUNT(DISTINCT record_id) AS recs, MAX(etl_can_populate) AS etl FROM field_mappings WHERE {usable_sql} GROUP BY target_field", (run_id,))}
    srcs: dict = defaultdict(dict)
    for r in c.execute(f"SELECT target_field, source_field, COUNT(*) FROM field_mappings WHERE {usable_sql} GROUP BY target_field, source_field", (run_id,)):
        srcs[r[0]][r[1]] = r[2]

    per_field = []
    for f in fields:
        st = per.get(f["field"])
        populated = st["recs"] if st else 0
        sample = None
        if populated:
            row = c.execute(f"SELECT target_value FROM field_mappings WHERE {usable_sql} AND target_field=? ORDER BY id LIMIT 1", (run_id, f["field"])).fetchone()
            sample = row[0] if row else None
        status = "missing" if populated == 0 else ("populated" if populated == n_records else "partial")
        per_field.append({
            "field": f["field"], "label": f["label"], "owner": f["owner"], "required": f["required"],
            "data_type": f["data_type"], "source_priority": f["source_priority"],
            "source_fields": sorted(srcs.get(f["field"], {})), "records_populated": populated, "records_total": n_records,
            "fill_rate": populated / n_records if n_records else 0.0, "value_available": populated > 0,
            "etl_can_populate": bool(st and st["etl"]) and populated > 0, "status": status, "sample_value": sample,
        })

    populated_fields = [p for p in per_field if p["value_available"]]
    etl_fields = [p for p in per_field if p["owner"] == "ETL"]
    etl_populated = [p for p in etl_fields if p["etl_can_populate"]]
    required = [f["field"] for f in fields if f["required"]]

    pairs = c.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT record_id, target_field FROM field_mappings WHERE {usable_sql})", (run_id,)).fetchone()[0]
    req_pairs = 0
    if required:
        marks = ",".join("?" * len(required))
        req_pairs = c.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT record_id, target_field FROM field_mappings WHERE {usable_sql} AND target_field IN ({marks}))",
                              (run_id, *required)).fetchone()[0]
    completeness = pairs / (n_records * total) if n_records and total else 0.0
    req_completeness = req_pairs / (n_records * len(required)) if n_records and required else 0.0

    ownership = []
    for owner in schema["owners"]:
        of = [p for p in per_field if p["owner"] == owner]
        ownership.append({"owner": owner, "total": len(of), "populated": sum(1 for p in of if p["value_available"]),
                          "missing": sum(1 for p in of if not p["value_available"])})

    missing = [{
        "field": p["field"], "label": p["label"], "owner": p["owner"], "required": p["required"],
        "reason": "No source field was mapped to this canonical field." if p["owner"] != "OTHER_SOURCE" else "No crawler data available.",
        "recommended_action": OWNER_ACTIONS[p["owner"]],
    } for p in per_field if not p["value_available"]]

    reverse = [{"field": p["field"], "label": p["label"],
                "sources": [{"source_field": k, "count": v} for k, v in sorted(srcs.get(p["field"], {}).items())]} for p in per_field]

    st = db.status_counts(c, run_id)
    top = db.rows(c, """SELECT source_field, target_field, owner, COUNT(*) AS n, AVG(confidence) AS confidence FROM field_mappings
                        WHERE mapping_run_id=? AND status='mapped' GROUP BY source_field, target_field, owner ORDER BY n DESC LIMIT 14""", (run_id,))
    canonical_mappings = c.execute("SELECT COUNT(*) FROM field_mappings WHERE mapping_run_id=? AND status='mapped'", (run_id,)).fetchone()[0]

    return {
        "metrics": {
            "profiles": n_records,
            "canonical_fields": total,
            "fields_extracted": st["mapped"] + st["unmapped"] + st["ambiguous"],
            "fields_mapped": st["mapped"], "fields_unmapped": st["unmapped"], "fields_ambiguous": st["ambiguous"],
            "canonical_mappings": canonical_mappings,
            "populated_fields": len(populated_fields), "missing_fields": total - len(populated_fields),
            "field_coverage": len(populated_fields) / total if total else 0.0,
            "etl_owned_total": len(etl_fields), "etl_owned_populated": len(etl_populated),
            "etl_coverage": len(etl_populated) / len(etl_fields) if etl_fields else 0.0,
            "profile_completeness": completeness,
            "required_completeness": req_completeness,
            "required_total": len(required),
        },
        "fields": per_field,
        "ownership": ownership,
        "missing": missing,
        "reverse_mapping": reverse,
        "top_mappings": top,
    }
