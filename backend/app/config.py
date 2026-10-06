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
SAMPLE_DATA_PATH = PROJECT_ROOT / "sample_data" / "real_sample_data.json"
APPROVED_SEED_PATH = PROJECT_ROOT / "sample_data" / "approved_examples_seed.json"
CANONICAL_SCHEMA_PATH = APP_DIR / "schemas" / "canonical_schema.json"
RESPONSE_SCHEMA_PATH = APP_DIR / "schemas" / "mapping_response.json"
PROMPT_PATH = APP_DIR / "prompts" / "schema_mapping.txt"

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium").strip()
PROMPT_VERSION = "4.0"
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

NORMALIZE_PROMPT_PATH = APP_DIR / "prompts" / "normalize_values.txt"
NORMALIZE_PROMPT_VERSION = "3.0"
# Optional cheaper model for the small "apply saved mapping to new records" requests (defaults to MODEL).
NORMALIZE_MODEL = os.getenv("ANTHROPIC_NORMALIZE_MODEL", "").strip() or MODEL
MAPPING_MODES = ("per_field", "per_field_relearn", "per_source", "per_source_relearn", "per_record")
FIELD_CHUNK = int(os.getenv("FIELD_CHUNK", "60"))  # unique source fields decided per Claude request in per_field mode

# Values a crawler emits to mean "no data". Treated as missing before mapping (raw data is kept untouched).
PLACEHOLDER_VALUES = {"", "-", "--", "---", "n/a", "na", "unknown", "none", "null", "nil", "undefined", "not available", "tbd"}

# ---- scale ----
APPLY_BATCH_SIZE = int(os.getenv("APPLY_BATCH_SIZE", "2000"))   # records mapped and written per batch
INGEST_BATCH_SIZE = 1000                                        # records stored per insert while streaming an upload
RAW_COPY_LIMIT = int(os.getenv("RAW_COPY_LIMIT", "2000"))       # runs up to this size copy raw JSON into source_records
SMALL_MODE_LIMIT = int(os.getenv("SMALL_MODE_LIMIT", "5000"))   # per_source / per_record hold all records in memory
MAX_WRAPPER_BYTES = 100 * 1024 * 1024                           # {"records": [...]} style JSON is read whole up to this size
MAX_RECORD_BYTES = 64 * 1024 * 1024                              # a single record larger than this is treated as invalid JSON

# Crawler bookkeeping blocks (crawler_meta, _meta, pipeline_info, ...) carry run ids, timestamps and extractor names.
# They never map to profile fields, so they are dropped from the mapping input (the stored raw record keeps them).
# Matching is on whole blocks (objects), never on single fields, so a field such as `crawled_from` (a profile URL) is kept.
DROP_METADATA_BLOCKS = os.getenv("DROP_METADATA_BLOCKS", "1") != "0"
EXTRA_METADATA_BLOCKS = {b.strip().lower() for b in os.getenv("EXTRA_METADATA_BLOCKS", "").split(",") if b.strip()}  # exact block names to also drop
