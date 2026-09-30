"""LLM schema mapper. Every semantic decision (what a source field means) comes from Claude.

This module only builds prompts, calls the API, and returns validated results. There are no
aliases, string-similarity checks, or regexes deciding what a field name means.

Two kinds of request:
  * map_record        - Claude decides what every field of one record means (full schema in the prompt).
  * normalize_batch   - the meaning is already decided (saved source mapping); Claude only derives the
                        per-record values for fields that need transforming (e.g. full name -> first name).
"""
import copy
import json
import time

import anthropic

from .. import config
from . import database as db
from .errors import MappingError
from .validator import ResponseValidationError, validate_response

_SYSTEM_PROMPT = config.PROMPT_PATH.read_text()
_NORMALIZE_PROMPT = config.NORMALIZE_PROMPT_PATH.read_text()
_RESPONSE_TEMPLATE = json.loads(config.RESPONSE_SCHEMA_PATH.read_text())


def make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(timeout=config.LLM_TIMEOUT_SECONDS, max_retries=2)


def build_response_schema(schema: dict) -> dict:
    """Constrain target_field to the canonical schema so the model cannot invent fields."""
    rs = copy.deepcopy(_RESPONSE_TEMPLATE)
    names = [f["field"] for f in schema["fields"]]
    rs["$defs"]["item"]["properties"]["target_field"] = {"anyOf": [{"type": "string", "enum": names}, {"type": "null"}]}
    return rs


def build_user_message(record: dict, record_index: int, schema: dict, examples: list[dict]) -> str:
    parts = [
        f"CANONICAL SCHEMA (schema_version {schema['schema_version']}). These are the ONLY valid target fields:",
        json.dumps(schema["fields"], indent=1),
    ]
    if examples:
        parts += [
            "APPROVED MAPPING EXAMPLES from past human review (context only, not rules; the record below decides):",
            json.dumps([{"source_field": e["source_field"], "target_field": e["target_field"], "example_value": e["example_value"]} for e in examples], indent=1),
        ]
    parts += [
        f"SOURCE RECORD (record_index {record_index}). Map every field in it:",
        json.dumps(record, indent=1, ensure_ascii=False),
    ]
    return "\n\n".join(parts)


def _log_call(run_id, record_index, purpose, attempt, model, prompt_version, status, request_text, response_text=None,
              error=None, stop_reason=None, usage=None, latency_ms=None):
    with db.conn() as c:
        c.execute(
            """INSERT INTO llm_calls(run_id, record_index, purpose, attempt, model, prompt_version, status, error, request_text,
               response_text, stop_reason, input_tokens, output_tokens, latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, record_index, purpose, attempt, model, prompt_version, status, error, request_text, response_text,
             stop_reason, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None), latency_ms, db.now()),
        )


def _request_json(client, *, run_id, record_index, purpose, model, prompt_version, system, user_text, response_schema,
                  validate, label, on_sent=None, on_response=None):
    """Call Claude for structured JSON, validate, and retry once with the validator's feedback."""
    messages = [{"role": "user", "content": user_text}]
    output_config = {"format": {"type": "json_schema", "schema": response_schema}}
    if config.EFFORT:
        output_config["effort"] = config.EFFORT

    last_error: ResponseValidationError | None = None
    for attempt in (1, 2):
        request_text = json.dumps(messages, ensure_ascii=False, indent=1)
        t0 = time.time()
        if on_sent:
            on_sent()
        try:
            resp = client.messages.create(model=model, max_tokens=config.MAX_TOKENS, system=system,
                                          messages=messages, output_config=output_config)
        except anthropic.APITimeoutError as e:
            _log_call(run_id, record_index, purpose, attempt, model, prompt_version, "error", request_text, error=f"timeout: {e}")
            raise MappingError("LLM_TIMEOUT", f"Claude timed out after {config.LLM_TIMEOUT_SECONDS:.0f}s ({label}).")
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            _log_call(run_id, record_index, purpose, attempt, model, prompt_version, "error", request_text, error=str(e))
            raise MappingError("LLM_API_FAILURE", f"Claude API error ({label}): {e}")
        latency = int((time.time() - t0) * 1000)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if on_response:
            on_response()

        if resp.stop_reason in ("refusal", "max_tokens"):
            _log_call(run_id, record_index, purpose, attempt, model, prompt_version, "invalid", request_text, text,
                      f"stop_reason={resp.stop_reason}", resp.stop_reason, resp.usage, latency)
            raise MappingError("MALFORMED_LLM_RESPONSE", f"Claude stopped with '{resp.stop_reason}' ({label}). If the response was cut off, lower FIELD_CHUNK.")
        try:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise ResponseValidationError("MALFORMED_LLM_RESPONSE", [f"response was not valid JSON: {e}"])
            result = validate(parsed)
        except ResponseValidationError as e:
            _log_call(run_id, record_index, purpose, attempt, model, prompt_version, "invalid", request_text, text,
                      "; ".join(e.errors), resp.stop_reason, resp.usage, latency)
            last_error = e
            messages = messages + [
                {"role": "assistant", "content": text or "{}"},
                {"role": "user", "content": "Your previous response failed validation:\n- " + "\n- ".join(e.errors) +
                 "\nReturn a corrected JSON response."},
            ]
            continue
        _log_call(run_id, record_index, purpose, attempt, model, prompt_version, "ok", request_text, text, None,
                  resp.stop_reason, resp.usage, latency)
        return result
    raise MappingError(last_error.code, f"{label}: invalid LLM response after retry: {last_error}")


def map_record(client, run_id: int, record_index: int, record: dict, schema: dict, examples: list[dict],
               thresholds: dict, on_sent=None, on_response=None) -> list[dict]:
    """Ask Claude what every field of one record means; validate; retry once."""
    schema_fields = {f["field"]: f for f in schema["fields"]}
    return _request_json(
        client, run_id=run_id, record_index=record_index, purpose="map", model=config.MODEL,
        prompt_version=config.PROMPT_VERSION, system=_SYSTEM_PROMPT,
        user_text=build_user_message(record, record_index, schema, examples),
        response_schema=build_response_schema(schema),
        validate=lambda parsed: validate_response(parsed, record, schema_fields, thresholds),
        label=f"mapping record {record_index + 1}", on_sent=on_sent, on_response=on_response)


def normalize_batch(client, run_id: int, items: list[tuple[int, dict]], decisions: list[dict], schema: dict,
                    on_sent=None, on_response=None) -> dict:
    """Derive per-record values for saved mappings that transform the source value.

    items: [(record_index, record)]. decisions: saved decisions with derived=True.
    Returns {(record_index, source_field, target_field): value_or_None}.
    """
    schema_fields = {f["field"]: f for f in schema["fields"]}
    # a record only needs values for the source fields it actually has
    expected = {(i, d["source_field"], d["target_field"]) for i, rec in items for d in decisions if d["source_field"] in rec}
    items = [(i, rec) for i, rec in items if any(k[0] == i for k in expected)]
    if not items:
        return {}
    used = {(sf, tf) for _, sf, tf in expected}
    decisions = [d for d in decisions if (d["source_field"], d["target_field"]) in used]
    targets = sorted({d["target_field"] for d in decisions})
    src_fields = sorted({d["source_field"] for d in decisions})

    response_schema = {
        "type": "object", "additionalProperties": False, "required": ["results"],
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["record_index", "source_field", "target_field", "target_value"],
            "properties": {
                "record_index": {"type": "integer"}, "source_field": {"type": "string"},
                "target_field": {"type": "string", "enum": targets},
                "target_value": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}}}}

    user_text = json.dumps({
        "mappings_to_apply": [{
            "source_field": d["source_field"], "target_field": d["target_field"],
            "target_field_description": schema_fields[d["target_field"]]["description"],
            "data_type": schema_fields[d["target_field"]]["data_type"],
            "item_fields": schema_fields[d["target_field"]].get("item_fields"),
            "worked_example": {"source_value": d["example_source_value"], "target_value": d["example_target_value"]},
        } for d in decisions],
        "records": [{"record_index": i, "values": {f: rec[f] for f in src_fields if f in rec}} for i, rec in items],
    }, indent=1, ensure_ascii=False)

    def validate(parsed):
        got, errors = {}, []
        res = parsed.get("results") if isinstance(parsed, dict) else None
        if not isinstance(res, list):
            raise ResponseValidationError("MALFORMED_LLM_RESPONSE", ["'results' must be an array"])
        for r in res:
            key = (r.get("record_index"), r.get("source_field"), r.get("target_field"))
            if key not in expected:
                errors.append(f"unexpected entry {key}")
            elif key in got:
                errors.append(f"duplicate entry {key}")
            else:
                got[key] = r.get("target_value")
        missing = expected - set(got)
        if missing:
            errors.append(f"missing entries: {sorted(missing, key=str)[:10]}")
        if errors:
            raise ResponseValidationError("MALFORMED_LLM_RESPONSE", errors)
        return got

    return _request_json(
        client, run_id=run_id, record_index=items[0][0], purpose="normalize", model=config.NORMALIZE_MODEL,
        prompt_version=config.NORMALIZE_PROMPT_VERSION, system=_NORMALIZE_PROMPT, user_text=user_text,
        response_schema=response_schema, validate=validate, label=f"normalizing {len(items)} records",
        on_sent=on_sent, on_response=on_response)


def build_fields_message(paths: list[str], index: dict, flats: list[dict], schema: dict, examples: list[dict]) -> str:
    parts = [
        f"CANONICAL SCHEMA (schema_version {schema['schema_version']}). These are the ONLY valid target fields:",
        json.dumps(schema["fields"], indent=1),
    ]
    if examples:
        parts += [
            "APPROVED MAPPING EXAMPLES from past human review (context only, not rules; the fields below decide):",
            json.dumps([{"source_field": e["source_field"], "target_field": e["target_field"], "example_value": e["example_value"]} for e in examples], indent=1),
        ]
    from . import sources
    parts += [
        "FIELDS TO MAP. Each entry is one distinct source field (a dotted path into the crawled record) seen across several records. "
        "Decide what each field means from its name, its sample values and the context records. "
        "For source_value and target_value use sample_values[0].",
        json.dumps([{"source_field": p, "records_with_field": len(index[p]["records"]), "sample_values": index[p]["samples"]} for p in paths],
                   indent=1, ensure_ascii=False),
        "CONTEXT RECORDS (whole records that contain some of these fields, for surrounding-field context only; do NOT map them):",
        json.dumps(sources.context_records(paths, flats, index), indent=1, ensure_ascii=False),
    ]
    return "\n\n".join(parts)


def map_fields_chunk(client, run_id: int, chunk_no: int, paths: list[str], index: dict, flats: list[dict], schema: dict,
                     examples: list[dict], thresholds: dict, on_sent=None, on_response=None) -> list[dict]:
    """Ask Claude what a group of distinct source fields means. Returns validated rows (one set per field)."""
    schema_fields = {f["field"]: f for f in schema["fields"]}
    sample_record = {p: index[p]["first_raw"] for p in paths}  # the raw value of sample #1 is authoritative
    return _request_json(
        client, run_id=run_id, record_index=chunk_no, purpose="map_fields", model=config.MODEL,
        prompt_version=config.PROMPT_VERSION, system=_SYSTEM_PROMPT,
        user_text=build_fields_message(paths, index, flats, schema, examples),
        response_schema=build_response_schema(schema),
        validate=lambda parsed: validate_response(parsed, sample_record, schema_fields, thresholds),
        label=f"mapping fields chunk {chunk_no + 1} ({len(paths)} fields)", on_sent=on_sent, on_response=on_response)
