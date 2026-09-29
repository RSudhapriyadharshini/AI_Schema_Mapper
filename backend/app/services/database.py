"""SQLite access layer. Every call opens its own connection (safe across threads)."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from ..config import DB_PATH, DEFAULT_SETTINGS

DDL = """
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  file_name TEXT NOT NULL,
  format TEXT NOT NULL,
  num_records INTEGER NOT NULL,
  num_fields INTEGER NOT NULL,
  fields_extracted INTEGER NOT NULL,
  dropped_empty INTEGER NOT NULL DEFAULT 0,
  crawler_version TEXT,
  extraction_prompt_version TEXT,
  records_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mapping_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  schema_version TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  source_file TEXT NOT NULL,
  status TEXT NOT NULL,
  records_processed INTEGER NOT NULL DEFAULT 0,
  fields_mapped INTEGER NOT NULL DEFAULT 0,
  fields_unmapped INTEGER NOT NULL DEFAULT 0,
  fields_ambiguous INTEGER NOT NULL DEFAULT 0,
  upload_id INTEGER,
  total_records INTEGER NOT NULL DEFAULT 0,
  crawler_version TEXT,
  extraction_prompt_version TEXT,
  thresholds_json TEXT,
  use_examples INTEGER NOT NULL DEFAULT 1,
  error_type TEXT,
  error_message TEXT
);
CREATE TABLE IF NOT EXISTS run_steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  step_key TEXT NOT NULL,
  label TEXT NOT NULL,
  position INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  detail TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(run_id, step_key)
);
CREATE TABLE IF NOT EXISTS source_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_run_id INTEGER NOT NULL,
  record_index INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_run_id INTEGER NOT NULL,
  record_id INTEGER NOT NULL,
  source_field TEXT NOT NULL,
  source_value TEXT,
  target_field TEXT,
  target_value TEXT,
  confidence REAL,
  status TEXT NOT NULL,
  owner TEXT,
  etl_can_populate INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  created_at TEXT NOT NULL,
  llm_status TEXT,
  review_state TEXT,
  validation_note TEXT
);
CREATE TABLE IF NOT EXISTS canonical_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mapping_run_id INTEGER NOT NULL,
  record_id INTEGER NOT NULL,
  canonical_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  record_index INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  model TEXT,
  prompt_version TEXT,
  status TEXT NOT NULL,
  error TEXT,
  request_text TEXT,
  response_text TEXT,
  stop_reason TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  latency_ms INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approved_mappings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_field TEXT NOT NULL,
  target_field TEXT NOT NULL,
  example_value TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(source_field, target_field)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

TABLES = ["mapping_runs", "source_records", "field_mappings", "canonical_records"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    """Transactional connection: commits on success, rolls back on error."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(DDL)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, str(v)))


def rows(c, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(c, sql: str, params=()) -> dict | None:
    r = c.execute(sql, params).fetchone()
    return dict(r) if r else None


# ---- settings -------------------------------------------------------------

def get_settings() -> dict:
    with conn() as c:
        return {r["key"]: float(r["value"]) for r in c.execute("SELECT key, value FROM settings")}


def set_settings(high: float, review: float) -> dict:
    with conn() as c:
        c.execute("REPLACE INTO settings(key, value) VALUES ('high_threshold', ?)", (str(high),))
        c.execute("REPLACE INTO settings(key, value) VALUES ('review_threshold', ?)", (str(review),))
    return get_settings()


def band(confidence, thresholds: dict) -> str | None:
    if confidence is None:
        return None
    if confidence >= thresholds["high_threshold"]:
        return "High"
    if confidence >= thresholds["review_threshold"]:
        return "Review"
    return "Ambiguous"


# ---- run steps ------------------------------------------------------------

STEPS = [
    ("upload", "Upload"),
    ("parse", "Parse"),
    ("detect_fields", "Detect Source Fields"),
    ("send_to_claude", "Send to Claude"),
    ("llm_mapping", "LLM Semantic Mapping"),
    ("validate", "Validate LLM Response"),
    ("canonical", "Generate Canonical Profile"),
    ("coverage", "Calculate Coverage"),
    ("write_db", "Write Draft Database"),
]


def set_step(run_id: int, key: str, status: str, detail: str | None = None) -> None:
    ts = now()
    with conn() as c:
        if status == "running":
            c.execute("UPDATE run_steps SET status=?, detail=COALESCE(?, detail), started_at=COALESCE(started_at, ?) WHERE run_id=? AND step_key=?",
                      (status, detail, ts, run_id, key))
        elif status in ("done", "failed"):
            c.execute("UPDATE run_steps SET status=?, detail=COALESCE(?, detail), started_at=COALESCE(started_at, ?), finished_at=? WHERE run_id=? AND step_key=?",
                      (status, detail, ts, ts, run_id, key))
        else:
            c.execute("UPDATE run_steps SET status=?, detail=COALESCE(?, detail) WHERE run_id=? AND step_key=?", (status, detail, run_id, key))


def run_label(run_id: int) -> str:
    return f"RUN-{run_id:05d}"


def refresh_run_counts(c, run_id: int) -> None:
    """Recompute source-field-level counts (mapped > ambiguous > unmapped per record field)."""
    r = rows(c, "SELECT record_id, source_field, status FROM field_mappings WHERE mapping_run_id=?", (run_id,))
    best: dict = {}
    rank = {"mapped": 3, "ambiguous": 2, "unmapped": 1}
    for x in r:
        k = (x["record_id"], x["source_field"])
        if rank[x["status"]] > rank.get(best.get(k, ""), 0):
            best[k] = x["status"]
    counts = {"mapped": 0, "ambiguous": 0, "unmapped": 0}
    for s in best.values():
        counts[s] += 1
    c.execute("UPDATE mapping_runs SET fields_mapped=?, fields_unmapped=?, fields_ambiguous=? WHERE id=?",
              (counts["mapped"], counts["unmapped"], counts["ambiguous"], run_id))
