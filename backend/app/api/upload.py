import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import config
from ..services import database as db
from ..services.errors import UploadError
from ..services.parser import flatten_record, parse_upload

router = APIRouter(prefix="/api")


def _store(file_name: str, content: bytes, crawler_version: str | None, extraction_prompt_version: str | None,
           source_name: str | None = None) -> dict:
    try:
        parsed = parse_upload(file_name, content)
    except UploadError as e:
        raise HTTPException(400, {"code": e.code, "message": e.message})
    records = parsed["records"]  # raw, exactly as uploaded
    flats = [flatten_record(r) for r in records]
    fields = {k for f, _ in flats for k in f}
    with db.conn() as c:
        cur = c.execute(
            """INSERT INTO uploads(created_at, file_name, format, num_records, num_fields, fields_extracted, dropped_empty,
               crawler_version, extraction_prompt_version, source_name, records_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), file_name, parsed["format"], len(records), len(fields), sum(len(f) for f, _ in flats),
             sum(d for _, d in flats), crawler_version or "unspecified", extraction_prompt_version or "unspecified",
             source_name or file_name, json.dumps(records, ensure_ascii=False)))
        upload_id = cur.lastrowid
    return get_upload(upload_id)


def get_upload(upload_id: int, with_records: bool = True) -> dict:
    with db.conn() as c:
        up = db.one(c, "SELECT * FROM uploads WHERE id=?", (upload_id,))
    if not up:
        raise HTTPException(404, "Upload not found.")
    records = json.loads(up.pop("records_json"))
    if with_records:
        counts: dict = {}
        for r in records:
            for k in flatten_record(r)[0]:
                counts[k] = counts.get(k, 0) + 1
        up["records"] = records
        up["fields"] = [{"field": k, "records": v} for k, v in sorted(counts.items())]
    return up


@router.post("/upload")
async def upload(file: UploadFile = File(...), crawler_version: str = Form(""), extraction_prompt_version: str = Form(""),
                 source_name: str = Form("")):
    return _store(file.filename or "upload", await file.read(), crawler_version.strip() or None,
                  extraction_prompt_version.strip() or None, source_name.strip() or None)


@router.post("/upload/sample")
def load_sample():
    return _store(config.SAMPLE_DATA_PATH.name, config.SAMPLE_DATA_PATH.read_bytes(),
                  config.SAMPLE_CRAWLER_VERSION, config.SAMPLE_EXTRACTION_PROMPT_VERSION)


@router.get("/uploads")
def list_uploads():
    with db.conn() as c:
        ups = db.rows(c, "SELECT id, created_at, file_name, format, num_records, num_fields, fields_extracted FROM uploads ORDER BY id DESC")
    return ups


@router.get("/uploads/{upload_id}")
def read_upload(upload_id: int):
    return get_upload(upload_id)
