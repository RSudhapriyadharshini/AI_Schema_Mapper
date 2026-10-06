"""Background processing pipeline. Step status is written to SQLite as work actually happens.

Scale design: the upload was streamed into SQLite (with a bounded summary of its distinct fields). A run
  1. asks Claude about distinct fields it has no saved decision for (a handful of requests, however many records),
  2. applies decisions to the records in bounded batches with plain code (copy / verified recipe),
  3. asks Claude for values only where no verified recipe exists (never in a no-LLM run),
  4. writes each batch as it is produced. Memory depends on the batch size, not on the number of records.
"""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from . import database as db
from . import llm_mapper, sources, uploads
from .coverage import build_canonical, coverage_from_db
from .errors import MappingError
from .parser import flatten_record

SMALL_MODES = ("per_source", "per_source_relearn", "per_record")


def create_run(upload_id: int, use_examples: bool, mapping_mode: str = "per_field", strict_no_llm: bool = False) -> int:
    if mapping_mode not in config.MAPPING_MODES:
        raise MappingError("INVALID_MODE", f"mapping_mode must be one of {config.MAPPING_MODES}.")
    if strict_no_llm and mapping_mode != "per_field":
        raise MappingError("INVALID_MODE", "A no-LLM run reuses saved field decisions, so it needs mapping_mode 'per_field'.")
    schema = config.load_canonical_schema()
    settings = db.get_settings()
    with db.conn() as c:
        up = db.one(c, "SELECT id, file_name, num_records, crawler_version, extraction_prompt_version, source_name FROM uploads WHERE id=?", (upload_id,))
        if not up:
            raise MappingError("NOT_FOUND", f"Upload {upload_id} not found.")
        cur = c.execute(
            """INSERT INTO mapping_runs(created_at, schema_version, model, prompt_version, source_file, status, upload_id,
               total_records, crawler_version, extraction_prompt_version, thresholds_json, use_examples, mapping_mode,
               source_name, schema_json, strict_no_llm)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), schema["schema_version"], config.MODEL, config.PROMPT_VERSION, up["file_name"], "running", upload_id,
             up["num_records"], up["crawler_version"], up["extraction_prompt_version"], json.dumps(settings), int(use_examples), mapping_mode,
             up.get("source_name") or up["file_name"], json.dumps(schema), int(strict_no_llm)))
        run_id = cur.lastrowid
        for i, (key, label) in enumerate(db.STEPS):
            c.execute("INSERT INTO run_steps(run_id, step_key, label, position) VALUES (?,?,?,?)", (run_id, key, label, i))
    return run_id


def start_run(upload_id: int, use_examples: bool = True, mapping_mode: str = "per_field", strict_no_llm: bool = False) -> int:
    run_id = create_run(upload_id, use_examples, mapping_mode, strict_no_llm)
    threading.Thread(target=execute_run, args=(run_id,), daemon=True, name=f"run-{run_id}").start()
    return run_id


def _create_source_records(run_id: int, up: dict, n: int) -> list[int]:
    """One source_records row per record. Small runs copy the raw JSON (as the spec's table shows); large runs
    reference the stored upload instead of duplicating hundreds of megabytes per run."""
    with db.conn() as c:
        if n <= config.RAW_COPY_LIMIT:
            for idx, raw in uploads.fetch_records(up, 0, n):
                c.execute("INSERT INTO source_records(mapping_run_id, record_index, raw_json, created_at) VALUES (?,?,?,?)",
                          (run_id, idx, json.dumps(raw, ensure_ascii=False), db.now()))
        else:
            c.execute("INSERT INTO source_records(mapping_run_id, record_index, raw_json, created_at) "
                      "SELECT ?, idx, '', ? FROM upload_records WHERE upload_id=? ORDER BY idx", (run_id, db.now(), up["id"]))
        return [r[0] for r in c.execute("SELECT id FROM source_records WHERE mapping_run_id=? ORDER BY record_index", (run_id,))]


def execute_run(run_id: int) -> None:
    state = {"step": "upload"}
    try:
        with db.conn() as c:
            run = db.one(c, "SELECT * FROM mapping_runs WHERE id=?", (run_id,))
            up = db.one(c, "SELECT * FROM uploads WHERE id=?", (run["upload_id"],))
            examples = db.rows(c, "SELECT source_field, target_field, example_value FROM approved_mappings ORDER BY id") if run["use_examples"] else []
        schema = json.loads(run["schema_json"])  # the schema snapshot this run was created with
        schema_fields = {f["field"]: f for f in schema["fields"]}
        examples = [e for e in examples if e["target_field"] in schema_fields]  # ignore examples for fields the schema no longer has
        thresholds = json.loads(run["thresholds_json"])
        mode = run["mapping_mode"] or "per_field"
        strict = bool(run["strict_no_llm"])
        source_name = run["source_name"] or up["file_name"]
        n = up["num_records"]
        if mode in SMALL_MODES and n > config.SMALL_MODE_LIMIT:
            raise MappingError("MODE_TOO_LARGE", f"Mode '{mode}' keeps every record in memory and supports up to {config.SMALL_MODE_LIMIT:,} records. Use 'per_field' for {n:,} records.")

        # Steps 1-3 happened at upload time; record what the upload actually produced.
        db.set_step(run_id, "upload", "done", f"{up['file_name']} received")
        db.set_step(run_id, "parse", "done", f"{up['format']}: {n:,} records parsed")
        db.set_step(run_id, "detect_fields", "done", f"{up['num_fields']:,} unique source fields, {up['fields_extracted']:,} values")

        record_ids = _create_source_records(run_id, up, n)  # raw data is preserved before any mapping happens
        client = None if strict else llm_mapper.make_client()
        lock = threading.Lock()
        counts = {"sent": 0, "received": 0, "validated": 0}
        state["step"] = "send_to_claude"
        db.set_step(run_id, "send_to_claude", "running", "no-LLM run: reusing saved decisions" if strict else f"planning ({mode})")
        db.set_step(run_id, "llm_mapping", "running", "waiting for Claude" if not strict else "not used in a no-LLM run")

        def on_sent():
            with lock:
                counts["sent"] += 1
                db.set_step(run_id, "send_to_claude", "running", f"{counts['sent']} request(s) sent to Claude")

        def on_response():
            with lock:
                counts["received"] += 1
                db.set_step(run_id, "llm_mapping", "running", f"{counts['received']} response(s) received")

        def validated(k):
            with lock:
                counts["validated"] += k
                db.set_step(run_id, "validate", "done" if counts["validated"] >= n else "running", f"{counts['validated']:,}/{n:,} records validated")

        def derive_and_apply(decs_by_idx: dict, flats: dict) -> dict:
            """Apply decisions to a batch of records. Copies and verified recipes are plain code; Claude is asked
            for a value only where a field needs transforming and has no verified recipe (never in a no-LLM run)."""
            need: dict = {}
            if not strict:
                for i, ds in decs_by_idx.items():
                    for d in sources.llm_value_decisions(ds):
                        need[(d["source_field"], d["target_field"])] = d
            by_rec: dict = {}
            if need:
                idxs = sorted(decs_by_idx)
                for k in range(0, len(idxs), sources.CHUNK):
                    chunk = [(i, flats[i]) for i in idxs[k:k + sources.CHUNK]]
                    for (ri, sf, tf), v in llm_mapper.normalize_batch(client, run_id, chunk, list(need.values()), schema, on_sent, on_response).items():
                        by_rec.setdefault(ri, {})[(sf, tf)] = v
            return {i: sources.rows_from_decisions(ds, flats[i], schema_fields, thresholds, by_rec.get(i, {}), strict)
                    for i, ds in decs_by_idx.items()}

        notes = {"text": ""}

        def per_field_batches():
            """Decide new fields with Claude (few requests), then yield batches of records mapped with plain code."""
            index = uploads.load_field_index(up)
            paths = list(index)
            # normal run: only decisions made with the current prompt version (a prompt change means re-learn).
            # no-LLM run: decisions made under any prompt version, because it exists to reuse what the source already has.
            found = sources.get_field_decisions(source_name, run["schema_version"], None if strict else run["prompt_version"]) if mode == "per_field" else {}
            if strict and not (found.keys() & set(paths)):
                have = sources.saved_sources(run["schema_version"])
                listing = "; ".join(f"'{s['source_name']}' ({s['fields']} fields)" for s in have) or "none"
                raise MappingError("NO_SAVED_DECISIONS", f"No saved field decisions for source name '{source_name}' match this file's fields (schema {run['schema_version']}). "
                                   f"Run it once without 'no-LLM run' using your API key, or use the same Source name as an earlier run. Saved sources: {listing}.")
            saved = {p: v["decisions"] for p, v in found.items()}
            versions = sorted({v["prompt_version"] for p, v in found.items() if p in set(paths)})
            by_path = {p: saved[p] for p in paths if p in saved}
            for p, ds in by_path.items():  # a saved decision may point at a schema field that has since been removed
                if any(d["target_field"] and d["target_field"] not in schema_fields for d in ds):
                    kept = [d for d in ds if not d["target_field"] or d["target_field"] in schema_fields]
                    by_path[p] = kept or [{"source_field": p, "target_field": None, "status": "unmapped", "confidence": 0.0,
                                           "reason": "The saved target field is no longer in the schema; re-learn this source.", "owner": None,
                                           "etl_can_populate": False, "derived": False, "recipe": None, "recipe_verified": False,
                                           "example_source_value": None, "example_target_value": None}]
            todo = [p for p in paths if p not in saved]
            if strict:
                # unseen fields are skipped, never guessed
                for p in todo:
                    by_path[p] = [{"source_field": p, "target_field": None, "status": "unmapped", "confidence": 0.0,
                                   "reason": "Not seen when this source was learned; skipped in a no-LLM run.", "owner": None,
                                   "etl_can_populate": False, "derived": False, "recipe": None, "recipe_verified": False,
                                   "example_source_value": None, "example_target_value": None}]
                chunks = []
            else:
                chunks = [todo[k:k + config.FIELD_CHUNK] for k in range(0, len(todo), config.FIELD_CHUNK)]
            if by_path:
                sources.mark_fields_used(source_name, [p for p in by_path if p in saved])
            new_by_path: dict = {}
            if chunks:
                load = lambda i: uploads.load_flat(up, i)  # noqa: E731
                contexts = [sources.pick_context(ch, index, load) for ch in chunks]
                done_chunks = 0
                with ThreadPoolExecutor(max_workers=max(1, min(config.MAPPER_CONCURRENCY, len(chunks)))) as ex:
                    futs = [ex.submit(llm_mapper.map_fields_chunk, client, run_id, ci, ch, index, contexts[ci], schema, examples, thresholds, on_sent, on_response)
                            for ci, ch in enumerate(chunks)]
                    try:
                        for f in as_completed(futs):
                            for d in sources.decisions_from_rows(f.result(), schema_fields):
                                new_by_path.setdefault(d["source_field"], []).append(d)
                            done_chunks += 1
                            db.set_step(run_id, "validate", "running", f"{done_chunks}/{len(chunks)} field-decision requests validated")
                    except Exception:
                        for f in futs:
                            f.cancel()
                        raise
                sources.save_field_decisions(source_name, new_by_path, run["model"], run["schema_version"], run["prompt_version"], run_id)
                by_path.update(new_by_path)
            kinds = sources.decision_kinds([d for ds in by_path.values() for d in ds])
            notes["text"] = (f"{len(paths):,} distinct source fields: {len(saved.keys() & set(paths)):,} reused from saved decisions, "
                             f"{len(todo):,} {'skipped (unseen, no-LLM run)' if strict else f'decided by Claude in {len(chunks)} request(s)'}. "
                             f"{'Saved decisions from prompt version(s) ' + ', '.join(versions) + '. ' if strict else ''}"
                             f"Mapped decisions: {kinds['copy']} copy, {kinds['recipe']} verified recipe, {kinds['llm']} need Claude for values")
            state["step"] = "canonical"
            for start in range(0, n, config.APPLY_BATCH_SIZE):
                batch = uploads.fetch_records(up, start, start + config.APPLY_BATCH_SIZE)
                flats = {i: flatten_record(raw)[0] for i, raw in batch}
                decs = {i: [d for p in flats[i] for d in by_path[p]] for i in flats}
                rows = derive_and_apply(decs, flats)
                validated(len(rows))
                yield sorted(rows), rows

        def small_mode_batches():
            """per_source / per_record: hold all records (bounded by SMALL_MODE_LIMIT), then yield in batches."""
            flats = {i: flatten_record(raw)[0] for i, raw in uploads.fetch_records(up, 0, n)}
            groups = [(None, [i]) for i in range(n)] if mode == "per_record" else sources.group_records([flats[i] for i in range(n)])
            use_saved, save_new = mode == "per_source", mode != "per_record"
            reused = {"n": 0}
            db.set_step(run_id, "send_to_claude", "running", f"{len(groups)} source group(s) for {n} records")

            def worker(group):
                sig, idxs = group
                out: dict[int, list[dict]] = {}
                saved_map = sources.get_saved(sig, run["schema_version"], run["prompt_version"]) if (sig and use_saved) else None
                if saved_map:
                    decisions, remaining = saved_map["decisions"], idxs
                    sources.mark_used(sig)
                    with lock:
                        reused["n"] += 1
                else:
                    i0 = idxs[0]
                    rows = llm_mapper.map_record(client, run_id, i0, flats[i0], schema, examples, thresholds, on_sent, on_response)
                    out[i0] = rows
                    validated(1)
                    decisions, remaining = sources.decisions_from_rows(rows, schema_fields), idxs[1:]
                    if sig and save_new:
                        sources.save(sig, list(flats[i0]), decisions, run["model"], run["schema_version"], run["prompt_version"], run_id)
                if remaining:
                    out.update(derive_and_apply({i: decisions for i in remaining}, flats))
                    validated(len(remaining))
                return out

            results: dict[int, list[dict]] = {}
            with ThreadPoolExecutor(max_workers=max(1, min(config.MAPPER_CONCURRENCY, len(groups)))) as ex:
                futures = [ex.submit(worker, g) for g in groups]
                try:
                    for f in as_completed(futures):
                        results.update(f.result())
                except Exception:
                    for f in futures:
                        f.cancel()
                    raise
            notes["text"] = f"{len(groups)} source group(s) for {n} records" + (f", {reused['n']} reused from saved mappings" if reused["n"] else "")
            state["step"] = "canonical"
            for start in range(0, n, config.APPLY_BATCH_SIZE):
                part = {i: results[i] for i in range(start, min(n, start + config.APPLY_BATCH_SIZE))}
                yield sorted(part), part

        meta = {"run_id": run_id, "model": run["model"], "schema_version": run["schema_version"], "prompt_version": run["prompt_version"]}
        written = {"records": 0, "rows": 0}

        def write_batch(idxs: list, rows_by_idx: dict) -> None:
            """Store one batch: mapping rows and canonical profiles, in one transaction. Ids are assigned up front
            so each canonical value can point at the exact mapping row it came from (lineage)."""
            now = db.now()
            with db.conn() as c:
                c.execute("BEGIN IMMEDIATE")  # serialises id assignment with any other run writing at the same time
                next_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM field_mappings").fetchone()[0]
                fm, cr = [], []
                for i in idxs:
                    rows = rows_by_idx[i]
                    for r in rows:
                        next_id += 1
                        r["id"] = next_id
                        fm.append((next_id, run_id, record_ids[i], r["source_field"], r["source_value"], r["target_field"], r["target_value"],
                                   r["confidence"], r["status"], r["owner"], int(r["etl_can_populate"]), r["reason"], now, r["llm_status"],
                                   r.get("review_state"), r["validation_note"], r.get("origin", "llm")))
                    cr.append((run_id, record_ids[i], json.dumps(build_canonical(rows, schema, meta), ensure_ascii=False), now))
                c.executemany("""INSERT INTO field_mappings(id, mapping_run_id, record_id, source_field, source_value, target_field, target_value,
                                 confidence, status, owner, etl_can_populate, reason, created_at, llm_status, review_state, validation_note, origin)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", fm)
                c.executemany("INSERT INTO canonical_records(mapping_run_id, record_id, canonical_json, created_at) VALUES (?,?,?,?)", cr)
            written["records"] += len(idxs)
            written["rows"] += len(fm)

        batches = per_field_batches() if mode in ("per_field", "per_field_relearn") else small_mode_batches()
        first = True
        for idxs, rows_by_idx in batches:
            if first:  # the first batch is ready: the Claude phase is over
                first = False
                db.set_step(run_id, "send_to_claude", "done", f"{counts['sent']} Claude request(s) for {n:,} records. {notes['text']}")
                db.set_step(run_id, "llm_mapping", "done", f"{counts['received']} response(s) received" if not strict else "not used in a no-LLM run")
                db.set_step(run_id, "canonical", "running", "building profiles in batches")
                db.set_step(run_id, "write_db", "running", "writing batches")
            state["step"] = "write_db"
            write_batch(idxs, rows_by_idx)
            state["step"] = "canonical"
            db.set_step(run_id, "canonical", "running", f"{written['records']:,}/{n:,} canonical profiles generated")
            db.set_step(run_id, "write_db", "running", f"{written['rows']:,} mappings, {written['records']:,} canonical records written")
        if first:  # nothing to write (should not happen: uploads always hold at least one record)
            raise MappingError("EMPTY_FILE", "There were no records to map.")
        db.set_step(run_id, "canonical", "done", f"{written['records']:,} canonical profiles generated")

        state["step"] = "coverage"
        db.set_step(run_id, "coverage", "running")
        with db.conn() as c:
            full = coverage_from_db(c, run_id, schema, n)
            cov = full["metrics"]
            c.execute("UPDATE mapping_runs SET coverage_json=? WHERE id=?", (json.dumps(full), run_id))  # cached; cleared when a review changes the mappings
        db.set_step(run_id, "coverage", "done", f"ETL coverage {cov['etl_coverage']:.0%}, completeness {cov['profile_completeness']:.0%}")

        state["step"] = "write_db"
        with db.conn() as c:
            db.refresh_run_counts(c, run_id)
            c.execute("UPDATE mapping_runs SET status='completed', records_processed=?, finished_at=? WHERE id=?", (n, db.now(), run_id))
        db.set_step(run_id, "write_db", "done", f"{written['rows']:,} mappings, {written['records']:,} canonical records written")
    except MappingError as e:
        _fail(run_id, state["step"], e.code, e.message)
    except sqlite3.Error as e:
        _fail(run_id, state["step"], "DATABASE_FAILURE", str(e))
    except Exception as e:  # noqa: BLE001 - surface anything unexpected on the run
        _fail(run_id, state["step"], "INTERNAL_ERROR", f"{type(e).__name__}: {e}")


def cleanup_partial(run_id: int) -> None:
    """Remove mapping results of a run that did not finish, so only fully valid results stay in the database.
    Raw source_records are kept: they are the input and never a mapping result."""
    with db.conn() as c:
        c.execute("DELETE FROM field_mappings WHERE mapping_run_id=?", (run_id,))
        c.execute("DELETE FROM canonical_records WHERE mapping_run_id=?", (run_id,))


def _fail(run_id: int, step: str, code: str, message: str) -> None:
    try:
        cleanup_partial(run_id)
        db.set_step(run_id, step, "failed", message)
        with db.conn() as c:
            c.execute("UPDATE mapping_runs SET status='failed', error_type=?, error_message=?, finished_at=? WHERE id=?",
                      (code, message, db.now(), run_id))
    except sqlite3.Error:
        pass
