"""Canonical profile construction and coverage metrics. Pure arithmetic over validated mappings."""
from collections import defaultdict

from .validator import usable, coerce_value

RANK = {"mapped": 3, "ambiguous": 2, "unmapped": 1}

OWNER_ACTIONS = {
    "ETL": "Improve or extend crawler/ETL extraction for this field.",
    "PROFESSIONAL": "Request completion from the professional.",
    "OTHER_SOURCE": "Obtain from the designated regulatory / internal source.",
    "SYSTEM": "Populated by the platform (system-generated).",
}


def schema_index(schema: dict) -> dict:
    return {f["field"]: f for f in schema["fields"]}


def build_canonical(mapping_rows: list[dict], schema: dict, meta: dict) -> dict:
    """Pick the best mapped value per canonical field. `meta` = run_id, model, schema_version, prompt_version."""
    best: dict = {}
    for r in mapping_rows:
        if r["status"] != "mapped" or not r.get("target_field") or not usable(r.get("target_value")):
            continue
        cur = best.get(r["target_field"])
        if cur is None or (r["confidence"] or 0) > (cur["confidence"] or 0):
            best[r["target_field"]] = r
    profile, lineage = {}, {}
    for f in schema["fields"]:
        r = best.get(f["field"])
        if not r:
            continue
        key = f["field"].split(".", 1)[1]
        profile[key] = coerce_value(r["target_value"], f["data_type"]) if f["data_type"] == "integer" else str(r["target_value"]).strip()
        lineage[key] = {
            "source_field": r["source_field"], "source_value": r["source_value"],
            "confidence": r["confidence"], "mapping_id": r.get("id"),
            "mapping_run": f"RUN-{meta['run_id']:05d}", "model": meta["model"],
            "schema_version": meta["schema_version"], "prompt_version": meta["prompt_version"],
            "review_state": r.get("review_state"),
        }
    return {"profile": profile, "lineage": lineage}


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
        key = f["field"].split(".", 1)[1]
        populated = sum(1 for c in canonicals if usable(c["profile"].get(key)))
        srcs: dict = defaultdict(set)
        for r in used[f["field"]]:
            srcs[r["source_field"]].add(r["record_index"])
        sample = next((c["profile"][key] for c in canonicals if usable(c["profile"].get(key))), None)
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
        n = sum(1 for f in fields if usable(c["profile"].get(f["field"].split(".", 1)[1])))
        n_req = sum(1 for f in fields if f["required"] and usable(c["profile"].get(f["field"].split(".", 1)[1])))
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
