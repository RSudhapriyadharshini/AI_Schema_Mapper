"""Background processing pipeline. Step status is written to SQLite as work actually happens."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from . import database as db
from . import llm_mapper
from .coverage import build_canonical, compute_coverage
from .errors import MappingError


def create_run(upload_id: int, use_examples: bool) -> int:
    schema = config.load_canonical_schema()
    settings = db.get_settings()
    with db.conn() as c:
        up = db.one(c, "SELECT * FROM uploads WHERE id=?", (upload_id,))
        if not up:
            raise MappingError("NOT_FOUND", f"Upload {upload_id} not found.")
        cur = c.execute(
            """INSERT INTO mapping_runs(created_at, schema_version, model, prompt_version, source_file, status, upload_id,
               total_records, crawler_version, extraction_prompt_version, thresholds_json, use_examples)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), schema["schema_version"], config.MODEL, config.PROMPT_VERSION, up["file_name"], "running", upload_id,
             up["num_records"], up["crawler_version"], up["extraction_prompt_version"], json.dumps(settings), int(use_examples)))
        run_id = cur.lastrowid
        for i, (key, label) in enumerate(db.STEPS):
            c.execute("INSERT INTO run_steps(run_id, step_key, label, position) VALUES (?,?,?,?)", (run_id, key, label, i))
    return run_id


def start_run(upload_id: int, use_examples: bool = True) -> int:
    run_id = create_run(upload_id, use_examples)
    threading.Thread(target=execute_run, args=(run_id,), daemon=True, name=f"run-{run_id}").start()
    return run_id


def execute_run(run_id: int) -> None:
    current = "upload"
    try:
        schema = config.load_canonical_schema()
        with db.conn() as c:
            run = db.one(c, "SELECT * FROM mapping_runs WHERE id=?", (run_id,))
            up = db.one(c, "SELECT * FROM uploads WHERE id=?", (run["upload_id"],))
            examples = db.rows(c, "SELECT source_field, target_field, example_value FROM approved_mappings ORDER BY id") if run["use_examples"] else []
        thresholds = json.loads(run["thresholds_json"])
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

        client = llm_mapper.make_client()
        lock = threading.Lock()
        counts = {"sent": 0, "received": 0, "validated": 0}
        current = "send_to_claude"
        db.set_step(run_id, "send_to_claude", "running", f"0/{n} requests sent")
        db.set_step(run_id, "llm_mapping", "running", f"0/{n} responses received")

        def worker(i):
            with lock:
                counts["sent"] += 1
                db.set_step(run_id, "send_to_claude", "done" if counts["sent"] == n else "running", f"{counts['sent']}/{n} requests sent to {config.MODEL}")

            def got_response():
                with lock:
                    counts["received"] += 1
                    db.set_step(run_id, "llm_mapping", "done" if counts["received"] == n else "running", f"{counts['received']}/{n} responses received")

            rows = llm_mapper.map_record(client, run_id, i, records[i], schema, examples, thresholds, on_response=got_response)
            with lock:
                counts["validated"] += 1
                db.set_step(run_id, "validate", "done" if counts["validated"] == n else "running", f"{counts['validated']}/{n} records validated")
            return i, rows

        results: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(config.MAPPER_CONCURRENCY, n))) as ex:
            futures = [ex.submit(worker, i) for i in range(n)]
            try:
                for f in as_completed(futures):
                    i, rows = f.result()
                    results[i] = rows
            except Exception:
                for f in futures:
                    f.cancel()
                raise

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
                       confidence, status, owner, etl_can_populate, reason, created_at, llm_status, review_state, validation_note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, record_ids[r["record_index"]], r["source_field"], r["source_value"], r["target_field"], r["target_value"],
                     r["confidence"], r["status"], r["owner"], int(r["etl_can_populate"]), r["reason"], now, r["llm_status"], None, r["validation_note"]))
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
