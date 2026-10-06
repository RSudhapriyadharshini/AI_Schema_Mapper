import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import config
from ..services import database as db
from ..services import parser, uploads
from ..services.errors import UploadError
from ..services.sources import FieldIndexBuilder

router = APIRouter(prefix="/api")

PREVIEW_RECORDS = 50
PREVIEW_FIELDS = 250


def _discard(upload_id: int) -> None:
    with db.conn() as c:
        for t in ("upload_records", "upload_fields"):
            c.execute(f"DELETE FROM {t} WHERE upload_id=?", (upload_id,))
        c.execute("DELETE FROM uploads WHERE id=?", (upload_id,))


def _ingest(file_name: str, fileobj, crawler_version: str | None, extraction_prompt_version: str | None,
            source_name: str | None = None) -> dict:
    """Stream a file into SQLite: raw records one batch at a time, plus a bounded summary of its distinct fields."""
    try:
        fmt = parser.format_name(file_name)
    except UploadError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message})
    with db.conn() as c:
        upload_id = c.execute(
            """INSERT INTO uploads(created_at, file_name, format, num_records, num_fields, fields_extracted, dropped_empty,
               crawler_version, extraction_prompt_version, source_name, records_json) VALUES (?,?,?,0,0,0,0,?,?,?,'[]')""",
            (db.now(), file_name, fmt, crawler_version or "unspecified", extraction_prompt_version or "unspecified",
             source_name or file_name)).lastrowid
    builder, n, extracted, dropped, batch = FieldIndexBuilder(), 0, 0, 0, []
    meta_stats: dict = {}

    def flush():
        if batch:
            with db.conn() as c:
                c.executemany("INSERT INTO upload_records(upload_id, idx, raw_json) VALUES (?,?,?)", batch)
            batch.clear()

    try:
        for raw in parser.iter_records(file_name, fileobj):
            flat, d = parser.flatten_record(raw, meta_stats)
            dropped += d
            if not flat:
                continue  # a record with no usable values carries nothing to map
            builder.add(n, flat)
            extracted += len(flat)
            batch.append((upload_id, n, json.dumps(raw, ensure_ascii=False)))
            n += 1
            if len(batch) >= config.INGEST_BATCH_SIZE:
                flush()
        flush()
        if n == 0:
            raise UploadError("EMPTY_FILE", "The file contains no records with usable values.")
        with db.conn() as c:
            c.executemany(
                "INSERT INTO upload_fields(upload_id, path, records, samples_json, first_raw, examples_json) VALUES (?,?,?,?,?,?)",
                [(upload_id, p, e["count"], json.dumps(e["samples"], ensure_ascii=False), e["first_raw"], json.dumps(e["examples"]))
                 for p, e in builder.idx.items()])
            c.execute("UPDATE uploads SET num_records=?, num_fields=?, fields_extracted=?, dropped_empty=?, metadata_ignored=?, metadata_blocks=? WHERE id=?",
                      (n, len(builder.idx), extracted, dropped, meta_stats.get("values", 0), json.dumps(meta_stats.get("blocks", {})), upload_id))
    except UploadError as e:
        _discard(upload_id)
        raise HTTPException(400, {"code": e.code, "message": e.message})
    except Exception:
        _discard(upload_id)
        raise
    return get_upload(upload_id)


def get_upload(upload_id: int) -> dict:
    with db.conn() as c:
        up = db.one(c, "SELECT * FROM uploads WHERE id=?", (upload_id,))
    if not up:
        raise HTTPException(404, "Upload not found.")
    up["records"] = [r for _, r in uploads.fetch_records(up, 0, PREVIEW_RECORDS)]
    up["records_shown"] = len(up["records"])
    index = uploads.load_field_index(up)
    up["fields"] = [{"field": p, "records": e["count"]} for p, e in sorted(index.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:PREVIEW_FIELDS]]
    up.pop("records_json", None)
    up["metadata_blocks"] = json.loads(up.get("metadata_blocks") or "{}")
    return up


@router.post("/upload")
def upload(file: UploadFile = File(...), crawler_version: str = Form(""), extraction_prompt_version: str = Form(""),
           source_name: str = Form("")):
    return _ingest(file.filename or "upload", file.file, crawler_version.strip() or None,
                   extraction_prompt_version.strip() or None, source_name.strip() or None)


@router.post("/upload/sample")
def load_sample():
    with open(config.SAMPLE_DATA_PATH, "rb") as f:
        return _ingest(config.SAMPLE_DATA_PATH.name, f, config.SAMPLE_CRAWLER_VERSION, config.SAMPLE_EXTRACTION_PROMPT_VERSION)


@router.get("/uploads")
def list_uploads():
    with db.conn() as c:
        return db.rows(c, "SELECT id, created_at, file_name, format, num_records, num_fields, fields_extracted FROM uploads ORDER BY id DESC")


@router.get("/uploads/{upload_id}")
def read_upload(upload_id: int):
    return get_upload(upload_id)
