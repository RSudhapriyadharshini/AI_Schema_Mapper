"""Parse uploaded crawler output (JSON or CSV) into a list of flat-ish records."""
import csv
import io
import json
from typing import Any

from .errors import UploadError


def _is_empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == [] or v == {}


def _clean_record(rec: dict) -> dict:
    return {str(k).strip(): v for k, v in rec.items() if str(k).strip() and not _is_empty(v)}


def parse_upload(file_name: str, content: bytes) -> dict:
    """Return {format, records, dropped_empty}. Raises UploadError on bad input."""
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

    dropped = 0
    records = []
    for rec in raw:
        cleaned = _clean_record(rec)
        dropped += len(rec) - len(cleaned)
        if cleaned:
            records.append(cleaned)
    if not records:
        raise UploadError("EMPTY_FILE", "All records were empty.")
    return {"format": fmt, "records": records, "dropped_empty": dropped}


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
