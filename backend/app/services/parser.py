"""Stream uploaded crawler output (JSON array, JSONL/NDJSON or CSV) and flatten nested records into dotted field paths."""
import codecs
import csv
import io
import json
import re
from typing import Any, BinaryIO, Iterator

from .. import config
from .errors import UploadError

CHUNK = 1 << 20  # bytes read at a time while streaming
_SKIP = re.compile(r"[ \t\r\n,]*")


def format_name(file_name: str) -> str:
    lower = file_name.lower()
    if lower.endswith((".jsonl", ".ndjson")):
        return "JSONL"
    if lower.endswith(".json"):
        return "JSON"
    if lower.endswith(".csv"):
        return "CSV"
    raise UploadError("UNSUPPORTED_FORMAT", "Only .json, .jsonl / .ndjson and .csv files are supported.")


def iter_records(file_name: str, f: BinaryIO) -> Iterator[dict]:
    """Yield raw records one at a time, exactly as uploaded. Nothing is held in memory beyond one read chunk."""
    fmt = format_name(file_name)
    if fmt == "JSONL":
        yield from _iter_jsonl(f)
    elif fmt == "JSON":
        yield from _iter_json(f)
    else:
        yield from _iter_csv(f)


def _iter_jsonl(f: BinaryIO) -> Iterator[dict]:
    for n, line in enumerate(f, 1):
        try:
            text = line.decode("utf-8-sig" if n == 1 else "utf-8").strip()
        except UnicodeDecodeError:
            raise UploadError("INVALID_ENCODING", "File is not valid UTF-8 text.")
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise UploadError("INVALID_JSON", f"Invalid JSON on line {n}: {e.msg}.")
        if not isinstance(obj, dict):
            raise UploadError("INVALID_JSON", f"Line {n}: each line must be a JSON object.")
        yield obj


def _iter_json(f: BinaryIO) -> Iterator[dict]:
    dec = codecs.getincrementaldecoder("utf-8-sig")()
    state = {"buf": "", "eof": False}

    def more() -> bool:
        chunk = f.read(CHUNK)
        try:
            if not chunk:
                state["buf"] += dec.decode(b"", final=True)
                state["eof"] = True
                return False
            state["buf"] += dec.decode(chunk)
        except UnicodeDecodeError:
            raise UploadError("INVALID_ENCODING", "File is not valid UTF-8 text.")
        return True

    while not state["buf"].strip() and more():
        pass
    buf = state["buf"].lstrip()
    if not buf:
        raise UploadError("EMPTY_FILE", "The uploaded file is empty.")
    if buf[0] == "{":  # a wrapper object or a single record: read it whole
        while more():
            if len(state["buf"]) > config.MAX_WRAPPER_BYTES:
                raise UploadError("FILE_TOO_LARGE", "A single JSON object this large is not supported. Use an array of records or JSONL.")
        yield from _from_object(state["buf"])
        return
    if buf[0] != "[":
        raise UploadError("INVALID_JSON", "JSON must be an array of record objects.")

    jd = json.JSONDecoder()
    buf, pos = buf[1:], 0
    state["buf"] = ""
    while True:
        pos = _SKIP.match(buf, pos).end()
        if pos >= len(buf):
            if state["eof"]:
                raise UploadError("INVALID_JSON", "Unexpected end of file: the array was never closed.")
            buf = buf[pos:] + ""
            pos = 0
            more()
            buf += state["buf"]
            state["buf"] = ""
            continue
        if buf[pos] == "]":
            return
        try:
            obj, end = jd.raw_decode(buf, pos)
        except json.JSONDecodeError as e:
            if state["eof"] or len(buf) - pos > config.MAX_RECORD_BYTES:
                raise UploadError("INVALID_JSON", f"Invalid JSON: {e.msg} (line {e.lineno}, column {e.colno}).")
            buf = buf[pos:]
            pos = 0
            more()
            buf += state["buf"]
            state["buf"] = ""
            continue
        if not isinstance(obj, dict):
            raise UploadError("INVALID_JSON", "JSON must be an array of record objects.")
        yield obj
        pos = end
        if pos > CHUNK:  # drop what has been consumed so the buffer stays about one chunk
            buf, pos = buf[pos:], 0


def _from_object(text: str) -> Iterator[dict]:
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as e:
        raise UploadError("INVALID_JSON", f"Invalid JSON: {e.msg} (line {e.lineno}, column {e.colno}).")
    lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    yield from (lists[0] if len(lists) == 1 else [data])


def _iter_csv(f: BinaryIO) -> Iterator[dict]:
    try:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))
        if not reader.fieldnames:
            raise UploadError("INVALID_CSV", "CSV has no header row.")
        for row in reader:
            if None in row:
                raise UploadError("INVALID_CSV", "CSV row has more values than header columns.")
            yield row
    except UnicodeDecodeError:
        raise UploadError("INVALID_ENCODING", "File is not valid UTF-8 text.")
    except csv.Error as e:
        raise UploadError("INVALID_CSV", f"Invalid CSV: {e}")


_TOKEN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
_BOOKKEEPING_PREFIX = {"pipeline", "crawl", "crawler", "crawled", "extraction", "extractor", "scrape", "scraper", "scraped", "run", "job", "fetch"}


def is_metadata_block(key: str) -> bool:
    """True for an object that holds crawler bookkeeping, e.g. crawler_meta, _meta, extraction-meta, pipelineInfo.

    Only the *name of a whole block* is considered, never a single field: `crawled_from` stays a field.
    """
    if not config.DROP_METADATA_BLOCKS:
        return False
    if key.strip().lower() in config.EXTRA_METADATA_BLOCKS:
        return True
    tokens = [t.lower() for t in _TOKEN.findall(key)]
    if not tokens:
        return False
    if "meta" in tokens or "metadata" in tokens:
        return True
    return len(tokens) == 2 and tokens[1] in ("info", "stats", "details") and tokens[0] in _BOOKKEEPING_PREFIX


def _count_leaves(v: Any) -> int:
    if isinstance(v, dict):
        return sum(_count_leaves(x) for x in v.values())
    return 1


def _prune_metadata(v: Any, stats: dict | None) -> Any:
    if not isinstance(v, dict):
        return v
    out = {}
    for k, x in v.items():
        if isinstance(x, dict) and is_metadata_block(str(k)):
            if stats is not None:
                stats["values"] = stats.get("values", 0) + _count_leaves(x)
                blocks = stats.setdefault("blocks", {})
                blocks[str(k)] = blocks.get(str(k), 0) + 1
            continue
        out[k] = _prune_metadata(x, stats)
    return out


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


def flatten_record(record: dict, stats: dict | None = None) -> tuple[dict, int]:
    """Return ({dotted.path: value}, dropped_count) for the mapping stage.

    Nested objects become dotted paths (``location.city``). Lists stay whole, because a list of objects
    (for example work history) has to be understood as a unit. Placeholders like "-" / "N/A" are dropped.
    """
    cleaned, dropped = _clean(_prune_metadata(record, stats))
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
