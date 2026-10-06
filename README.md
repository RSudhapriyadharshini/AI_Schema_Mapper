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
| `ANTHROPIC_NORMALIZE_MODEL` | optional, model for the small value-derivation requests (defaults to `ANTHROPIC_MODEL`) |
| `FIELD_CHUNK` | optional, distinct fields decided per request in per_field mode, default 60 (lower it if a response is cut off) |
| `APPLY_BATCH_SIZE` | optional, records mapped and written per batch, default 2000 |
| `RAW_COPY_LIMIT` | optional, runs up to this size copy raw JSON into `source_records`, default 2000 |
| `SMALL_MODE_LIMIT` | optional, max records for the per_source / per_record modes, default 5000 |
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

Try editing `sample_data/real_sample_data.json` (or upload your own file), e.g. rename a field or add
`random_unknown_field`, and re-run: the mapping changes accordingly.

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
`backend/app/prompts/{schema_mapping,normalize_values}.txt`. Output is constrained with Structured Outputs (`output_config.format`
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

## Canonical schema (version 2.0)

`backend/app/schemas/canonical_schema.json` holds 40 strict fields: the person fields (`name`, `title`, `about`, `phone`,
`email1`, `positions`, `licenses`, ...) and `company_*` fields that are assembled into `profile.company[0]`. Each field has a
description, data type, required flag, owner and source priority. List-typed fields (`string_list`, `object_list`) are
exchanged with Claude as JSON-encoded strings and type-checked (`object_list` entries may only use the field's `item_fields`).
Owners, required flags and the sub-fields of `operating_hours` (`day`, `open`, `close`) are editable defaults.

Each run stores a snapshot of the schema it used, so old runs stay explainable after the schema changes. Runs created
before snapshots existed cannot be displayed; start a new run or delete `backend/data/draft.db*`.

## Input handling

* Upload `.json` (array of records), `.jsonl` / `.ndjson` (one record per line) or `.csv`. Files are **streamed**: they are read in chunks and
  stored record by record, so file size is limited by disk, not memory. A JSON file that is one big `{"records": [...]}` object is read whole (up to 100 MB).
* Records are stored exactly as uploaded (`upload_records`). Nothing is overwritten.
* While streaming, the app builds a bounded summary of the source: each distinct field path, how many records have it, and up to five
  sample values (`upload_fields`). That summary, not the records, is what Claude is shown.
* For mapping, each record is **flattened**: nested objects become dotted paths (`location_info.town`,
  `record.agentname`); lists stay whole (work history, social links and licenses are understood as units).
* **Crawler metadata is dropped early.** Bookkeeping blocks (`crawler_meta`, `_meta`, `extraction_meta`, `pipeline_info`, `run_info`, ...) hold run
  ids, timestamps and extractor names that never map to profile fields. They are removed from the *mapping input* (so they are never sent to Claude and
  never create mapping rows) but stay in the stored raw record. Matching is on the name of a whole **block**, never a single field, so `crawled_from`
  (a profile URL) is kept. The Source Data page lists what was ignored. Set `DROP_METADATA_BLOCKS=0` to turn it off or
  `EXTRA_METADATA_BLOCKS=name1,name2` to add exact block names. See `is_metadata_block` in `services/parser.py`.
* Placeholder values that mean "no data" (`""`, `-`, `N/A`, `unknown`, `null`, ...; list in `config.PLACEHOLDER_VALUES`) are ignored
  for mapping and counted on the Source Data page. This is data cleaning, not a decision about what a field means.

## Cost control: mapping modes

The dropdown on the run panel chooses how many Claude requests a run makes:

| Mode | What Claude is asked | Best when |
|---|---|---|
| **Map each distinct field once** (`per_field`, default) | Every distinct source field path across all records is decided once, with 3-5 sample values and two whole context records. Decisions are saved per source name and reused on later runs. | many records share field names, or the same source is crawled repeatedly |
| `per_field_relearn` | Same, ignoring saved decisions | after changing model, prompt or schema |
| Map one sample record per source (`per_source`) | One whole sample record per field-name signature, reused for the other records | records come in a few clean templates |
| `per_source_relearn` | Same, ignoring saved mappings | |
| Map every record (`per_record`) | One full mapping request per record | baseline for comparing quality and cost |

How reuse works without hard-coding meaning: Claude's decision (source field, target field, status, confidence, reason) is
stored. Fields whose value already fits the target type are copied. Fields that need transforming (splitting a string into a list,
"12 years" to 12, an address into parts, a list of objects into `positions`) go to Claude in batched requests (20 records per
request) that carry only those values, not the schema. Each reused mapping is type-checked and subject to the confidence
thresholds, and shows its origin. Human review decisions are written back into the saved decisions, so a correction is reused next time.
Saved decisions are keyed to the source name, schema version and prompt version. A different source name is decided again by Claude;
decisions are never guessed across sources. Up to `FIELD_CHUNK` (default 60) fields are decided per request.

Your mileage depends on the data: `per_field` and `per_source` save most when field names repeat. A file where every record
uses its own field names (like `sample_data/real_sample_data.json`, 675 distinct paths across 30 records) saves little on a first run, and
`per_source` saves nothing there. The saving then comes from saved decisions on later runs of the same source. The run panel shows the
Claude requests and tokens each run used, so compare modes on your own file.

## Cheaper requests

* **Slim answers.** Claude is no longer asked to echo the source value, the owner or the ETL flag: the code already knows them (raw data, schema owner,
  `source_priority`). `target_value` is `null` when the value is used unchanged (only trimmed); it is given only when the value is transformed.
  Reasons are one short sentence. In your real run about 30% of the answer text was those echoes.
* **Prompt caching.** Every mapping request starts with the same system prompt and schema block, which is marked for caching
  (`cache_control`); everything that varies (approved examples, the fields, the context records) comes after it. The Mapping Logs tab shows
  cache read / written tokens per request and the run panel shows the totals, so you can confirm it works: the first request writes the cache and later
  ones read it. If the cached part is below the model's minimum size or more than 5 minutes pass between requests, nothing is cached (you will see 0).

## Recipes and no-LLM runs

When Claude decides that a field maps to a schema field and the value has to be reshaped (a comma-separated string into a list,
"12+ years" into 12, a list of jobs into `positions`, the Facebook entry of a list of social links into `facebook_link`), it also returns a
**recipe**: a short list of steps from a small closed vocabulary (`split_list`, `first_number`, `find_in_list`, `map_objects`, ...; see
`services/recipes.py`). Claude writes the recipe once; ordinary code runs it on every record.

A recipe is trusted only if it **reproduces Claude's own answer for the sample value**. Each saved decision is one of:

| Kind | Applied by |
|---|---|
| copy (value already fits the target type) | plain code |
| transform with a verified recipe | plain code (recipe) |
| transform without a verified recipe | Claude, in batched requests (skipped in a no-LLM run) |

**No-LLM run** (checkbox on the run panel, `strict_no_llm` in the API): reuse the saved decisions and verified recipes of a source and make
**zero Claude requests**. It does not even need an API key. Fields the source has never shown are left unmapped, with a reason; values that
need Claude and have no verified recipe stay empty, with a note on the mapping row. Nothing is guessed. The Saved field decisions card
shows, per source, how many decisions have verified recipes and how many still need Claude.

## Scale

Designed so memory depends on the *batch size* and the *number of distinct fields*, not on the number of records:

1. **Ingest** streams the file into SQLite in batches (`INGEST_BATCH_SIZE`).
2. **Decide**: Claude decides only the distinct fields that have no saved decision, up to `FIELD_CHUNK` per request. 200,000 records from one
   template cost the same number of field-decision requests as 30.
3. **Apply**: records are read back in batches (`APPLY_BATCH_SIZE`, default 2,000), mapped with plain code, and written batch by batch.
4. **Coverage and counts** are SQL aggregates over the stored rows, not Python loops.
5. **Failure**: partial results of a run that fails are deleted, so only complete runs leave mapping results behind. Raw records are kept.
6. **API and UI** are paged (mappings, canonical profiles, a grouped review queue). "Apply to all N records" on a review card updates every
   affected record and the saved decision.
7. Runs above `RAW_COPY_LIMIT` (2,000) records reference the stored upload instead of copying raw JSON into `source_records`.
   `per_source` / `per_record` modes keep all records in memory and are limited to `SMALL_MODE_LIMIT` (5,000).

Measure it on your machine (Claude is replaced by a small stub, so this tests the pipeline, not mapping quality):

```bash
cd backend && .venv/bin/python scripts/scale_check.py 20000
```

What is still prototype-grade: SQLite (one writer), the job runs in the API process (a restart fails the run; it does not resume), and about one
mapping row per source field per record is stored, which is large at millions of records. The next steps for production are Postgres, a job
queue with checkpoints, and storing only exceptions per record.

## How coverage is calculated

* **Field coverage** = canonical fields with a usable value in ≥ 1 profile ÷ all canonical fields.
* **ETL coverage** = ETL-owned fields populated from the crawler (`etl_can_populate`) ÷ ETL-owned fields.
  `etl_can_populate` is computed in code: the mapping is valid and the crawler is listed in the field's `source_priority` in the schema.
* **Profile completeness** = mean over profiles of (fields with usable values ÷ all canonical fields). Required-only completeness is shown too.
* Ownership breakdown, missing fields (with owner-based recommended action) and reverse mapping are computed from the stored mappings.

## Project structure

```
backend/app/{main.py, config.py}
backend/app/api/{upload,mapping,records,schema}.py
backend/app/services/{parser,llm_mapper,validator,coverage,pipeline,sources,recipes,uploads,database,errors}.py
backend/scripts/scale_check.py
backend/app/prompts/{schema_mapping,normalize_values}.txt
backend/app/schemas/{canonical_schema,mapping_response}.json
backend/tests/test_pipeline.py, test_modes.py, test_scale.py, test_slim.py  # offline tests using a STUB Claude client (test-only)
frontend/src/{App.tsx, pages/, components/, services/api.ts}
sample_data/{real_sample_data.json (loaded by the sample button), approved_examples_seed.json, older small samples}
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
