"""Parse uploaded crawler output (JSON or CSV), and flatten nested records into dotted field paths."""
import csv
import io
import json
from typing import Any

from .. import config
from .errors import UploadError


def parse_upload(file_name: str, content: bytes) -> dict:
    """Return {format, records}. Records are returned exactly as uploaded (raw data is never altered)."""
    if not content or not content.strip():
        raise UploadError("EMPTY_FILE", "The uploaded file is empty.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError("INVALID_ENCODING", "File is not valid UTF-8 text.")

    lower = file_name.lower()
    if lower.endswith(".json"):
        fmt, raw = "JSON", _parse_json(text)
    elif lower.endswith(".csv"):
        fmt, raw = "CSV", _parse_csv(text)
    else:
        raise UploadError("UNSUPPORTED_FORMAT", "Only .json and .csv files are supported.")

    if not raw:
        raise UploadError("EMPTY_FILE", "The file contains no records.")
    records = [r for r in raw if flatten_record(r)[0]]
    if not records:
        raise UploadError("EMPTY_FILE", "All records were empty.")
    return {"format": fmt, "records": records}


def _clean(v: Any):
    """Drop placeholder / empty values recursively. Returns (cleaned_or_None, number_of_leaf_values_dropped)."""
    if isinstance(v, str):
        s = v.strip()
        return (None, 1) if s.lower() in config.PLACEHOLDER_VALUES else (s, 0)
    if isinstance(v, dict):
        out, dropped = {}, 0
        for k, x in v.items():
            c, d = _clean(x)
            dropped += d
            if c is not None and str(k).strip():
                out[str(k).strip()] = c
        return (out or None), dropped
    if isinstance(v, list):
        out, dropped = [], 0
        for x in v:
            c, d = _clean(x)
            dropped += d
            if c is not None:
                out.append(c)
        return (out or None), dropped
    return (None, 1) if v is None else (v, 0)


def flatten_record(record: dict) -> tuple[dict, int]:
    """Return ({dotted.path: value}, dropped_count) for the mapping stage.

    Nested objects become dotted paths (``location.city``). Lists stay whole, because a list of objects
    (for example work history) has to be understood as a unit. Placeholders like "-" / "N/A" are dropped.
    """
    cleaned, dropped = _clean(record)
    flat: dict = {}

    def walk(v, prefix):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{prefix}.{k}" if prefix else k)
        else:
            flat[prefix] = v

    if isinstance(cleaned, dict):
        walk(cleaned, "")
    return flat, dropped


def _parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UploadError("INVALID_JSON", f"Invalid JSON: {e.msg} (line {e.lineno}, column {e.colno}).")
    if isinstance(data, dict):
        # tolerate {"records": [...]} style wrappers, else a single record
        lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
        data = lists[0] if len(lists) == 1 else [data]
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise UploadError("INVALID_JSON", "JSON must be an array of record objects.")
    return data


def _parse_csv(text: str) -> list[dict]:
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise UploadError("INVALID_CSV", "CSV has no header row.")
        rows = []
        for row in reader:
            if None in row:
                raise UploadError("INVALID_CSV", "CSV row has more values than header columns.")
            rows.append(row)
        return rows
    except csv.Error as e:
        raise UploadError("INVALID_CSV", f"Invalid CSV: {e}")
