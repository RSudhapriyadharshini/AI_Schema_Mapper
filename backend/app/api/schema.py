from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config
from ..services import database as db

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"ok": True, "api_key_configured": config.api_key_configured(), "model": config.MODEL,
            "prompt_version": config.PROMPT_VERSION, "effort": config.EFFORT or None}


@router.get("/schema")
def get_schema():
    return config.load_canonical_schema()


class Settings(BaseModel):
    high_threshold: float
    review_threshold: float


@router.get("/settings")
def get_settings():
    return db.get_settings()


@router.put("/settings")
def put_settings(s: Settings):
    if not (0 <= s.review_threshold < s.high_threshold <= 1):
        raise HTTPException(400, "Thresholds must satisfy 0 <= review < high <= 1.")
    return db.set_settings(s.high_threshold, s.review_threshold)
