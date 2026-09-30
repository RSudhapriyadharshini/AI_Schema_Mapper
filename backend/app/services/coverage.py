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


def source_field_status(mapping_rows: list[dict]) -> dict:
    """(record_index, source_field) -> best status."""
    best: dict = {}
    for r in mapping_rows:
        k = (r["record_index"], r["source_field"])
        if RANK[r["status"]] > RANK.get(best.get(k, ""), 0):
            best[k] = r["status"]
    return best


def compute_coverage(schema: dict, mapping_rows: list[dict], canonicals: list[dict], num_records: int) -> dict:
    """mapping_rows need record_index; canonicals is the list of {"profile": ..., "lineage": ...}."""
    fields = schema["fields"]
    total = len(fields)

    used = defaultdict(list)  # target_field -> mapped rows whose value is usable
    for r in mapping_rows:
        if r["status"] == "mapped" and r.get("target_field") and usable(r.get("target_value")):
            used[r["target_field"]].append(r)

    per_field = []
    for f in fields:
        key = f["field"]
        populated = sum(1 for c in canonicals if usable(c["flat"].get(key)))
        srcs: dict = defaultdict(set)
        for r in used[f["field"]]:
            srcs[r["source_field"]].add(r["record_index"])
        sample = display_value(next((c["flat"][key] for c in canonicals if usable(c["flat"].get(key))), None))
        etl_ok = any(r["etl_can_populate"] for r in used[f["field"]])
        status = "missing" if populated == 0 else ("populated" if populated == num_records else "partial")
        per_field.append({
            "field": f["field"], "label": f["label"], "owner": f["owner"], "required": f["required"],
            "data_type": f["data_type"], "source_priority": f["source_priority"],
            "source_fields": sorted(srcs), "records_populated": populated, "records_total": num_records,
            "fill_rate": populated / num_records if num_records else 0.0,
            "value_available": populated > 0, "etl_can_populate": etl_ok and populated > 0,
            "status": status, "sample_value": sample,
        })

    populated_fields = [p for p in per_field if p["value_available"]]
    etl_fields = [p for p in per_field if p["owner"] == "ETL"]
    etl_populated = [p for p in etl_fields if p["etl_can_populate"]]
    required = [p for p in per_field if p["required"]]

    rec_scores = []
    for c in canonicals:
        n = sum(1 for f in fields if usable(c["flat"].get(f["field"])))
        n_req = sum(1 for f in fields if f["required"] and usable(c["flat"].get(f["field"])))
        rec_scores.append({"populated": n, "total": total, "required_populated": n_req, "required_total": len(required)})
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    completeness = avg([s["populated"] / total for s in rec_scores])
    req_completeness = avg([s["required_populated"] / s["required_total"] for s in rec_scores if s["required_total"]])

    ownership = []
    for owner in schema["owners"]:
        of = [p for p in per_field if p["owner"] == owner]
        ownership.append({"owner": owner, "total": len(of), "populated": sum(1 for p in of if p["value_available"]),
                          "missing": sum(1 for p in of if not p["value_available"])})

    missing = [{
        "field": p["field"], "label": p["label"], "owner": p["owner"], "required": p["required"],
        "reason": "No source field was mapped to this canonical field." if p["owner"] != "OTHER_SOURCE"
        else "No crawler data available.",
        "recommended_action": OWNER_ACTIONS[p["owner"]],
    } for p in per_field if not p["value_available"]]

    reverse = []
    for p in per_field:
        counts: dict = defaultdict(int)
        for r in used[p["field"]]:
            counts[r["source_field"]] += 1
        reverse.append({"field": p["field"], "label": p["label"], "sources": [{"source_field": k, "count": v} for k, v in sorted(counts.items())]})

    sf = source_field_status(mapping_rows)
    st = defaultdict(int)
    for s in sf.values():
        st[s] += 1

    return {
        "metrics": {
            "profiles": num_records,
            "canonical_fields": total,
            "fields_extracted": len(sf),
            "fields_mapped": st["mapped"], "fields_unmapped": st["unmapped"], "fields_ambiguous": st["ambiguous"],
            "canonical_mappings": sum(1 for r in mapping_rows if r["status"] == "mapped"),
            "populated_fields": len(populated_fields), "missing_fields": total - len(populated_fields),
            "field_coverage": len(populated_fields) / total if total else 0.0,
            "etl_owned_total": len(etl_fields), "etl_owned_populated": len(etl_populated),
            "etl_coverage": len(etl_populated) / len(etl_fields) if etl_fields else 0.0,
            "profile_completeness": completeness,
            "required_completeness": req_completeness,
            "required_total": len(required),
        },
        "record_scores": rec_scores,
        "fields": per_field,
        "ownership": ownership,
        "missing": missing,
        "reverse_mapping": reverse,
    }
