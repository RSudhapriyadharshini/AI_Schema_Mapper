"""Deterministic validation of the LLM's response. Never decides semantics; only checks contracts."""
import json
import re
from typing import Any

from .errors import MappingError


class ResponseValidationError(Exception):
    """Structural problem with an LLM response. Triggers one retry with feedback."""

    def __init__(self, code: str, errors: list[str]):
        super().__init__("; ".join(errors))
        self.code = code
        self.errors = errors


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"^https?://\S+$", re.I)


def usable(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return str(v).strip() != ""


def _json_of(s: str, kind):
    try:
        v = json.loads(s)
    except json.JSONDecodeError:
        raise ValueError("value is not valid JSON")
    if not isinstance(v, kind):
        raise ValueError(f"expected a JSON {'array' if kind is list else 'object'}")
    return v


def coerce_value(value: Any, data_type: str, item_fields: list[str] | None = None):
    """Type-validate a normalized value. Returns the typed value or raises ValueError.

    string / email / url -> str; integer -> int; number -> float;
    string_list -> list[str]; object_list -> list[dict] limited to item_fields.
    List types are exchanged as JSON-encoded strings.
    """
    s = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value).strip()
    if data_type == "integer":
        if not re.fullmatch(r"\d{1,3}", s):
            raise ValueError(f"'{s}' is not a whole number")
        return int(s)
    if data_type == "number":
        try:
            n = float(s)
        except ValueError:
            raise ValueError(f"'{s}' is not a number")
        if n != n or n in (float("inf"), float("-inf")):
            raise ValueError(f"'{s}' is not a finite number")
        return n
    if data_type == "email":
        if not EMAIL_RE.match(s):
            raise ValueError(f"'{s}' is not a valid email")
        return s
    if data_type == "url":
        if not URL_RE.match(s):
            raise ValueError(f"'{s}' is not a valid http(s) URL")
        return s
    if data_type == "string_list":
        out, seen = [], set()
        for x in _json_of(s, list):
            if isinstance(x, (list, dict)) or not usable(x):
                continue
            t = str(x).strip()
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        if not out:
            raise ValueError("list has no usable items")
        return out
    if data_type == "object_list":
        allowed = item_fields or []
        out = []
        for x in _json_of(s, list):
            if not isinstance(x, dict):
                raise ValueError("list entries must be objects")
            unknown = [k for k in x if k not in allowed]
            if unknown:
                raise ValueError(f"unknown keys {unknown}; allowed keys are {allowed}")
            item = {k: str(v).strip() for k, v in x.items() if usable(v) and not isinstance(v, (list, dict))}
            if item:
                out.append(item)
        if not out:
            raise ValueError("list has no usable entries")
        return out
    return s


def to_text(typed: Any) -> str:
    """Canonical text form of a typed value (lists as JSON)."""
    if isinstance(typed, (list, dict)):
        return json.dumps(typed, ensure_ascii=False)
    return str(typed)


def is_copy(raw: str, target_value: Any, field: dict) -> bool:
    """True when copying the raw source value gives the same typed value the LLM produced."""
    try:
        a = coerce_value(raw, field["data_type"], field.get("item_fields"))
        b = coerce_value(target_value, field["data_type"], field.get("item_fields"))
    except ValueError:
        return False
    return a == b


def source_value_str(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def validate_response(parsed: Any, record: dict, schema_fields: dict, thresholds: dict) -> list[dict]:
    """Return validated mapping rows for one record or raise ResponseValidationError."""
    errors: list[str] = []
    invalid_field = False
    if not isinstance(parsed, dict):
        raise ResponseValidationError("MALFORMED_LLM_RESPONSE", ["top level must be a JSON object"])
    for key in ("mappings", "unmapped_fields", "ambiguous_fields"):
        if not isinstance(parsed.get(key), list):
            errors.append(f"'{key}' must be an array")
    if errors:
        raise ResponseValidationError("MALFORMED_LLM_RESPONSE", errors)

    rows: list[dict] = []
    for list_name, expected in (("mappings", "mapped"), ("unmapped_fields", "unmapped"), ("ambiguous_fields", "ambiguous")):
        for it in parsed[list_name]:
            if not isinstance(it, dict):
                errors.append(f"{list_name}: entry is not an object")
                continue
            sf = it.get("source_field")
            if sf not in record:
                errors.append(f"{list_name}: source_field '{sf}' does not exist in the record")
                continue
            if it.get("status") != expected:
                errors.append(f"{list_name}: '{sf}' has status '{it.get('status')}', expected '{expected}'")
                continue
            conf = it.get("confidence")
            if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
                errors.append(f"'{sf}': confidence must be a number between 0 and 1")
                continue
            tf = it.get("target_field")
            if tf is not None and tf not in schema_fields:
                errors.append(f"'{sf}': target_field '{tf}' is not in the canonical schema")
                invalid_field = True
                continue
            if expected == "mapped" and (tf is None or not usable(it.get("target_value"))):
                errors.append(f"'{sf}': a mapped entry needs a target_field and a non-empty target_value")
                continue
            if expected == "unmapped":
                tf = None
            rows.append({
                "source_field": sf,
                "source_value": source_value_str(record[sf]),  # raw value is authoritative
                "target_field": tf,
                "target_value": it.get("target_value") if expected == "mapped" else None,
                "llm_target_value": it.get("target_value") if expected == "mapped" else None,
                "llm_etl_can_populate": bool(it.get("etl_can_populate")) and expected == "mapped",
                "origin": "llm",
                "confidence": float(conf),
                "status": expected,
                "llm_status": expected,
                "reason": str(it.get("reason") or ""),
                "owner": schema_fields[tf]["owner"] if tf else None,
                "etl_can_populate": bool(it.get("etl_can_populate")) and expected == "mapped",
                "validation_note": None,
            })

    covered = {r["source_field"] for r in rows}
    missing = [f for f in record if f not in covered]
    if missing:
        errors.append(f"source fields missing from the response: {missing}")
    if errors:
        raise ResponseValidationError("INVALID_CANONICAL_FIELD" if invalid_field else "MALFORMED_LLM_RESPONSE", errors)

    # A source field with a mapped entry wins over conflicting unmapped/ambiguous entries.
    mapped_fields = {r["source_field"] for r in rows if r["status"] == "mapped"}
    rows = [r for r in rows if r["status"] == "mapped" or r["source_field"] not in mapped_fields]

    for r in rows:
        if r["status"] == "mapped":
            finalize_mapped_row(r, schema_fields, thresholds)
    return rows


def finalize_mapped_row(r: dict, schema_fields: dict, thresholds: dict, human_approved: bool = False) -> None:
    """Type-check the value and apply the confidence threshold to one mapped row (in place)."""
    try:
        fld = schema_fields[r["target_field"]]
        r["target_value"] = to_text(coerce_value(r["target_value"], fld["data_type"], fld.get("item_fields")))
    except ValueError as e:
        _demote(r, f"type validation failed: {e}")
        return
    if not human_approved and r["confidence"] < thresholds["review_threshold"]:
        _demote(r, f"LLM-reported confidence {r['confidence']:.2f} is below the review threshold {thresholds['review_threshold']:.2f}")


def demote(row: dict, note: str) -> None:
    _demote(row, note)


def _demote(row: dict, note: str) -> None:
    row["status"] = "ambiguous"
    row["target_value"] = None
    row["etl_can_populate"] = False
    row["validation_note"] = note


def require_rows(rows: list[dict]) -> None:
    if not rows:
        raise MappingError("MALFORMED_LLM_RESPONSE", "The LLM returned no mappings.")
