"""Background processing pipeline. Step status is written to SQLite as work actually happens."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from . import database as db
from . import llm_mapper, sources
from .coverage import build_canonical, compute_coverage
from .errors import MappingError


def create_run(upload_id: int, use_examples: bool, mapping_mode: str = "per_source") -> int:
    if mapping_mode not in config.MAPPING_MODES:
        raise MappingError("INVALID_MODE", f"mapping_mode must be one of {config.MAPPING_MODES}.")
    schema = config.load_canonical_schema()
    settings = db.get_settings()
    with db.conn() as c:
        up = db.one(c, "SELECT * FROM uploads WHERE id=?", (upload_id,))
        if not up:
            raise MappingError("NOT_FOUND", f"Upload {upload_id} not found.")
        cur = c.execute(
            """INSERT INTO mapping_runs(created_at, schema_version, model, prompt_version, source_file, status, upload_id,
               total_records, crawler_version, extraction_prompt_version, thresholds_json, use_examples, mapping_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), schema["schema_version"], config.MODEL, config.PROMPT_VERSION, up["file_name"], "running", upload_id,
             up["num_records"], up["crawler_version"], up["extraction_prompt_version"], json.dumps(settings), int(use_examples), mapping_mode))
        run_id = cur.lastrowid
        for i, (key, label) in enumerate(db.STEPS):
            c.execute("INSERT INTO run_steps(run_id, step_key, label, position) VALUES (?,?,?,?)", (run_id, key, label, i))
    return run_id


def start_run(upload_id: int, use_examples: bool = True, mapping_mode: str = "per_source") -> int:
    run_id = create_run(upload_id, use_examples, mapping_mode)
    threading.Thread(target=execute_run, args=(run_id,), daemon=True, name=f"run-{run_id}").start()
    return run_id


def execute_run(run_id: int) -> None:
    current = "upload"
    try:
        schema = config.load_canonical_schema()
        schema_fields = {f["field"]: f for f in schema["fields"]}
        with db.conn() as c:
            run = db.one(c, "SELECT * FROM mapping_runs WHERE id=?", (run_id,))
            up = db.one(c, "SELECT * FROM uploads WHERE id=?", (run["upload_id"],))
            examples = db.rows(c, "SELECT source_field, target_field, example_value FROM approved_mappings ORDER BY id") if run["use_examples"] else []
        thresholds = json.loads(run["thresholds_json"])
        mode = run["mapping_mode"] or "per_source"
        records = json.loads(up["records_json"])
        n = len(records)

        # Steps 1-3 happened at upload time; record what the upload actually produced.
        db.set_step(run_id, "upload", "done", f"{up['file_name']} received")
        db.set_step(run_id, "parse", "done", f"{up['format']}: {n} records parsed")
        db.set_step(run_id, "detect_fields", "done", f"{up['num_fields']} unique source fields, {up['fields_extracted']} values")

        # Preserve raw data first; mapping never overwrites it.
        with db.conn() as c:
            record_ids = []
            for i, rec in enumerate(records):
                cur = c.execute("INSERT INTO source_records(mapping_run_id, record_index, raw_json, created_at) VALUES (?,?,?,?)",
                                (run_id, i, json.dumps(rec, ensure_ascii=False), db.now()))
                record_ids.append(cur.lastrowid)

        # Plan: which records does Claude map itself, and which reuse a decision?
        if mode == "per_record":
            groups = [(None, [i]) for i in range(n)]
        else:
            groups = sources.group_records(records)
        use_saved, save_new = mode == "per_source", mode != "per_record"
        client = llm_mapper.make_client()
        lock = threading.Lock()
        counts = {"sent": 0, "received": 0, "groups": 0, "validated": 0, "reused_groups": 0}
        current = "send_to_claude"
        db.set_step(run_id, "send_to_claude", "running", f"{len(groups)} source group(s) for {n} records")
        db.set_step(run_id, "llm_mapping", "running", "waiting for Claude")

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
                db.set_step(run_id, "validate", "done" if counts["validated"] == n else "running", f"{counts['validated']}/{n} records validated")

        def worker(group):
            sig, idxs = group
            out: dict[int, list[dict]] = {}
            saved = sources.get_saved(sig, run["schema_version"], run["prompt_version"]) if (sig and use_saved) else None
            if saved:
                decisions, remaining = saved["decisions"], idxs
                sources.mark_used(sig)
                with lock:
                    counts["reused_groups"] += 1
            else:
                i0 = idxs[0]
                rows = llm_mapper.map_record(client, run_id, i0, records[i0], schema, examples, thresholds, on_sent, on_response)
                out[i0] = rows
                validated(1)
                decisions, remaining = sources.decisions_from_rows(rows), idxs[1:]
                if sig and save_new:
                    sources.save(sig, list(records[i0]), decisions, run["model"], run["schema_version"], run["prompt_version"], run_id)
            if remaining:
                derived = sources.derived_decisions(decisions)
                values: dict = {}
                if derived:
                    for k in range(0, len(remaining), sources.CHUNK):
                        chunk = [(i, records[i]) for i in remaining[k:k + sources.CHUNK]]
                        values.update(llm_mapper.normalize_batch(client, run_id, chunk, derived, schema, on_sent, on_response))
                for i in remaining:
                    dv = {(sf, tf): v for (ri, sf, tf), v in values.items() if ri == i}
                    out[i] = sources.rows_from_decisions(decisions, records[i], schema_fields, thresholds, dv)
                validated(len(remaining))
            with lock:
                counts["groups"] += 1
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
        summary = (f"{counts['sent']} Claude request(s) for {n} records" + (f", {counts['reused_groups']} of {len(groups)} source(s) reused from saved mappings" if counts["reused_groups"] else ""))
        db.set_step(run_id, "send_to_claude", "done", summary)
        db.set_step(run_id, "llm_mapping", "done", f"{counts['received']} response(s) received")

        meta = {"run_id": run_id, "model": run["model"], "schema_version": run["schema_version"], "prompt_version": run["prompt_version"]}
        current = "canonical"
        db.set_step(run_id, "canonical", "running")
        all_rows = [dict(r, record_index=i) for i in range(n) for r in results[i]]
        canon = [build_canonical([r for r in all_rows if r["record_index"] == i], schema, meta) for i in range(n)]
        db.set_step(run_id, "canonical", "done", f"{n} canonical profiles generated")

        current = "coverage"
        db.set_step(run_id, "coverage", "running")
        cov = compute_coverage(schema, all_rows, canon, n)["metrics"]
        db.set_step(run_id, "coverage", "done", f"ETL coverage {cov['etl_coverage']:.0%}, completeness {cov['profile_completeness']:.0%}")

        current = "write_db"
        db.set_step(run_id, "write_db", "running")
        with db.conn() as c:  # single transaction: nothing is written unless everything is valid
            now = db.now()
            for r in all_rows:
                cur = c.execute(
                    """INSERT INTO field_mappings(mapping_run_id, record_id, source_field, source_value, target_field, target_value,
                       confidence, status, owner, etl_can_populate, reason, created_at, llm_status, review_state, validation_note, origin)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, record_ids[r["record_index"]], r["source_field"], r["source_value"], r["target_field"], r["target_value"],
                     r["confidence"], r["status"], r["owner"], int(r["etl_can_populate"]), r["reason"], now, r["llm_status"], r.get("review_state"),
                     r["validation_note"], r.get("origin", "llm")))
                r["id"] = cur.lastrowid
            for i in range(n):
                cj = build_canonical([r for r in all_rows if r["record_index"] == i], schema, meta)
                c.execute("INSERT INTO canonical_records(mapping_run_id, record_id, canonical_json, created_at) VALUES (?,?,?,?)",
                          (run_id, record_ids[i], json.dumps(cj, ensure_ascii=False), now))
            db.refresh_run_counts(c, run_id)
            c.execute("UPDATE mapping_runs SET status='completed', records_processed=?, finished_at=? WHERE id=?", (n, now, run_id))
        db.set_step(run_id, "write_db", "done", f"{len(all_rows)} mappings, {n} canonical records written")
    except MappingError as e:
        _fail(run_id, current, e.code, e.message)
    except sqlite3.Error as e:
        _fail(run_id, current, "DATABASE_FAILURE", str(e))
    except Exception as e:  # noqa: BLE001 - surface anything unexpected on the run
        _fail(run_id, current, "INTERNAL_ERROR", f"{type(e).__name__}: {e}")


def _fail(run_id: int, step: str, code: str, message: str) -> None:
    try:
        db.set_step(run_id, step, "failed", message)
        with db.conn() as c:
            c.execute("UPDATE mapping_runs SET status='failed', error_type=?, error_message=?, finished_at=? WHERE id=?",
                      (code, message, db.now(), run_id))
    except sqlite3.Error:
        pass
