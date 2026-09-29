"""LLM schema mapper. Every semantic decision (what a source field means) comes from Claude.

This module only builds the prompt, calls the API, and returns validated results. There are no
aliases, string-similarity checks, or regexes deciding what a field name means.
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


def _log_call(run_id, record_index, attempt, status, request_text, response_text=None, error=None,
              stop_reason=None, usage=None, latency_ms=None):
    with db.conn() as c:
        c.execute(
            """INSERT INTO llm_calls(run_id, record_index, attempt, model, prompt_version, status, error, request_text,
               response_text, stop_reason, input_tokens, output_tokens, latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, record_index, attempt, config.MODEL, config.PROMPT_VERSION, status, error, request_text, response_text,
             stop_reason, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None), latency_ms, db.now()),
        )


def map_record(client, run_id: int, record_index: int, record: dict, schema: dict, examples: list[dict],
               thresholds: dict, on_response=None) -> list[dict]:
    """Ask Claude to map one record; validate; retry once with validator feedback."""
    schema_fields = {f["field"]: f for f in schema["fields"]}
    response_schema = build_response_schema(schema)
    user_text = build_user_message(record, record_index, schema, examples)
    messages = [{"role": "user", "content": user_text}]
    output_config = {"format": {"type": "json_schema", "schema": response_schema}}
    if config.EFFORT:
        output_config["effort"] = config.EFFORT

    last_error: ResponseValidationError | None = None
    for attempt in (1, 2):
        request_text = json.dumps(messages, ensure_ascii=False, indent=1)
        t0 = time.time()
        try:
            resp = client.messages.create(model=config.MODEL, max_tokens=config.MAX_TOKENS, system=_SYSTEM_PROMPT,
                                          messages=messages, output_config=output_config)
        except anthropic.APITimeoutError as e:
            _log_call(run_id, record_index, attempt, "error", request_text, error=f"timeout: {e}")
            raise MappingError("LLM_TIMEOUT", f"Claude timed out after {config.LLM_TIMEOUT_SECONDS:.0f}s on record {record_index + 1}.")
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            _log_call(run_id, record_index, attempt, "error", request_text, error=str(e))
            raise MappingError("LLM_API_FAILURE", f"Claude API error on record {record_index + 1}: {e}")
        latency = int((time.time() - t0) * 1000)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        if on_response:
            on_response()

        if resp.stop_reason in ("refusal", "max_tokens"):
            _log_call(run_id, record_index, attempt, "invalid", request_text, text, f"stop_reason={resp.stop_reason}",
                      resp.stop_reason, resp.usage, latency)
            raise MappingError("MALFORMED_LLM_RESPONSE", f"Claude stopped with '{resp.stop_reason}' on record {record_index + 1}.")
        try:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise ResponseValidationError("MALFORMED_LLM_RESPONSE", [f"response was not valid JSON: {e}"])
            rows = validate_response(parsed, record, schema_fields, thresholds)
        except ResponseValidationError as e:
            _log_call(run_id, record_index, attempt, "invalid", request_text, text, "; ".join(e.errors), resp.stop_reason, resp.usage, latency)
            last_error = e
            messages = messages + [
                {"role": "assistant", "content": text or "{}"},
                {"role": "user", "content": "Your previous response failed validation:\n- " + "\n- ".join(e.errors) +
                 "\nReturn a corrected JSON response for the same record."},
            ]
            continue
        _log_call(run_id, record_index, attempt, "ok", request_text, text, None, resp.stop_reason, resp.usage, latency)
        return rows
    raise MappingError(last_error.code, f"Record {record_index + 1}: invalid LLM response after retry: {last_error}")
