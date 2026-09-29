# AI Schema Mapping + ETL Coverage Prototype

A local, working prototype: crawler output with inconsistent field names goes in; **Claude** decides what each
source field means and maps it to a fixed canonical profile schema; the app then computes ETL coverage,
profile completeness, ownership breakdown and missing fields, and stores raw → mapping → canonical data in SQLite.

```
Crawler data → Parse → Claude (semantic mapping) → Validate → Canonical profile → Coverage → Draft DB (SQLite)
                                                                                    └─ future: dedup → enrichment
```

## What is (and is not) hard-coded

* Hard-coded: the **sample input** (`sample_data/`) and the **canonical schema** (`backend/app/schemas/canonical_schema.json`).
* Not hard-coded: every mapping, confidence, reason, owner and normalized value. They come from the Claude API.
  There are no aliases, regexes, string-similarity rules or canned responses in the app.
* Deterministic code only does: parsing, JSON/type validation, coverage arithmetic, DB writes and UI.

## Prerequisites

* Python 3.11+
* Node.js 20+ and npm
* An Anthropic API key

## Environment variables

Copy `.env.example` to `.env` in the project root and set:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | required, never committed |
| `ANTHROPIC_MODEL` | optional, default `claude-opus-5-5` |
| `ANTHROPIC_EFFORT` | optional, default `medium` (blank to omit; needed for models without effort support) |
| `MAPPER_CONCURRENCY` | optional, parallel Claude calls, default 4 |
| `LLM_TIMEOUT_SECONDS` | optional, default 180 |

## Installation

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd ../frontend && npm install
```

## Backend startup (port 8000)

```bash
cd backend && .venv/bin/uvicorn app.main:app --port 8000
```

## Frontend startup (port 3000)

```bash
cd frontend && npm run dev
```

Open <http://localhost:3000>.

## How to run

1. Open the **Dashboard** (or **Source Data**) and click **Load Sample Crawler Data**, or upload your own `.json` / `.csv`.
2. Click **Run AI Schema Mapping**. The pipeline strip shows real backend job status (polled every 600 ms).
3. Explore **AI Mapping** (flow, table, click a row to explain, review queue), **Canonical Profile** (with lineage),
   **Coverage Analysis**, **Draft Database**, **Mapping Logs** (raw Claude request/response) and **Canonical Schema**.

Try editing `sample_data/sample_crawler_data.json`, e.g. rename `agent_name` to `representative_name` or add
`random_unknown_field`, re-load and re-run: the mapping changes accordingly.

## Database

SQLite at `backend/data/draft.db` (git-ignored). Required tables: `mapping_runs`, `source_records`,
`field_mappings`, `canonical_records`. Supporting tables: `uploads`, `run_steps` (job status), `llm_calls` (logs),
`approved_mappings` (mapping memory), `settings` (thresholds).

* **Raw is never overwritten**: `source_records.raw_json` is written first; mappings and canonical records are derived and stored separately.
* **Nothing invalid is written**: all validated mappings/canonical records for a run go in one transaction at the end. A failed run keeps only its raw records and log.
* **Lineage**: each canonical field records source field, source value, confidence, model, schema version, prompt version and run (`RUN-00001`).

## How mapping works

For each source record, one Claude call receives: the canonical schema (name, description, type, required, owner,
source priority), optional human-approved examples (context only), and the full record. The system prompt is in
`backend/app/prompts/schema_mapping.txt`. Output is constrained with Structured Outputs (`output_config.format`
JSON schema) whose `target_field` is an enum of the canonical fields, so Claude cannot invent one.

Validation (`services/validator.py`) then checks: JSON shape, every source field accounted for, target in schema,
confidence in [0,1], values type-checked (integer/email/URL). The raw source value always overrides the model's echo.
On a structural failure the model gets the errors and one retry; then the run fails with a typed error
(`MALFORMED_LLM_RESPONSE`, `INVALID_CANONICAL_FIELD`, `LLM_TIMEOUT`, `LLM_API_FAILURE`, `DATABASE_FAILURE`).
A mapping whose type check fails, or whose confidence is below the review threshold, is downgraded to *ambiguous*.

Confidence is **LLM-reported**, not a calibrated probability. Bands (configurable on the Canonical Schema page):
≥ 0.90 High, ≥ 0.70 Review, below = Ambiguous.

Manual review: approve (choose a canonical field), reject or skip ambiguous/unmapped fields. Approved mappings are stored and
sent to Claude as examples on later runs; Claude still makes the decision.

## How coverage is calculated

* **Field coverage** = canonical fields with a usable value in ≥ 1 profile ÷ all canonical fields.
* **ETL coverage** = ETL-owned fields populated from the crawler (Claude: `etl_can_populate`) ÷ ETL-owned fields.
  `etl_can_populate` means the mapping is valid and the crawler is an accepted source per the field's `source_priority`.
* **Profile completeness** = mean over profiles of (fields with usable values ÷ all canonical fields). Required-only completeness is shown too.
* Ownership breakdown, missing fields (with owner-based recommended action) and reverse mapping are computed from the stored mappings.

## Project structure

```
backend/app/{main.py, config.py}
backend/app/api/{upload,mapping,records,schema}.py
backend/app/services/{parser,llm_mapper,validator,coverage,pipeline,database,errors}.py
backend/app/prompts/schema_mapping.txt
backend/app/schemas/{canonical_schema,mapping_response}.json
backend/tests/test_pipeline.py     # offline tests using a STUB Claude client (test-only)
frontend/src/{App.tsx, pages/, components/, services/api.ts}
sample_data/{sample_crawler_data.json, approved_examples_seed.json}
```

## Tests

```bash
cd backend && .venv/bin/python -m pytest -q tests
```

These run the pipeline against a stub client to test parsing, validation, retry, coverage math, transactional
writes and review. They do **not** test Claude's mapping quality; run the app with a real key for that.

## Troubleshooting

* **"ANTHROPIC_API_KEY is not set"**: add it to `.env` in the project root and restart the backend.
* **`LLM_API_FAILURE` with a 400 about `effort`/`output_config`**: your `ANTHROPIC_MODEL` may not support them; set `ANTHROPIC_EFFORT=` (blank) or use a current model.
* **UI says it cannot reach the backend**: start uvicorn on port 8000; Vite proxies `/api` there.
* **A run shows as failed with `INTERRUPTED`**: the backend restarted mid-run; start a new run.
* **Reset everything**: stop the backend and delete `backend/data/draft.db*`.
