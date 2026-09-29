"""Central configuration: paths, versions, and environment-driven settings."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(BACKEND_DIR / ".env")

DB_PATH = Path(os.getenv("DB_PATH", BACKEND_DIR / "data" / "draft.db"))
SAMPLE_DATA_PATH = PROJECT_ROOT / "sample_data" / "sample_crawler_data.json"
APPROVED_SEED_PATH = PROJECT_ROOT / "sample_data" / "approved_examples_seed.json"
CANONICAL_SCHEMA_PATH = APP_DIR / "schemas" / "canonical_schema.json"
RESPONSE_SCHEMA_PATH = APP_DIR / "schemas" / "mapping_response.json"
PROMPT_PATH = APP_DIR / "prompts" / "schema_mapping.txt"

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium").strip()
PROMPT_VERSION = "1.0"
MAPPER_CONCURRENCY = int(os.getenv("MAPPER_CONCURRENCY", "4"))
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))
MAX_TOKENS = 16000

# Provenance recorded for the bundled sample file (uploads can supply their own).
SAMPLE_CRAWLER_VERSION = "sample-crawler-1.0"
SAMPLE_EXTRACTION_PROMPT_VERSION = "sample-extract-1.0"

DEFAULT_SETTINGS = {"high_threshold": 0.90, "review_threshold": 0.70}


def load_canonical_schema() -> dict:
    return json.loads(CANONICAL_SCHEMA_PATH.read_text())


def api_key_configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
