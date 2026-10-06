"""Scale behaviour: streaming ingest, batched apply, recipes, no-LLM runs, paging and cleanup.

Uses the same STUB Claude client as the other tests (test-only scaffolding).
"""
import json

import pytest
from test_pipeline import StubClient, _run, _upload, client  # noqa: F401  (client is a fixture)

REC = lambda n, e, y, s: {"agent_name": n, "phone": "555", "email": e, "years": y, "skills": s}  # noqa: E731
THREE = [REC("Alice Smith", "a@x.com", "8 years", "Buyer, Seller"), REC("Bob Jones", "b@x.com", "12+ years", "Land"),
         REC("Cy Lee", "c@x.com", "3 years", "Luxury, Condo")]


def _use(monkeypatch, stub):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)


def _profiles(client, run_id):
    return [r["profile"] for r in client.get(f"/api/runs/{run_id}/canonical").json()["records"]]


# ---- recipes -------------------------------------------------------------

def test_verified_recipes_remove_claude_from_value_reshaping(client, monkeypatch):
    stub = StubClient(recipes="good")
    _use(monkeypatch, stub)
    run_id, st = _run(client, _upload(client, THREE, "site")["id"])
    assert st["run"]["status"] == "completed", st
    assert (stub.field_calls, stub.norm_calls) == (1, 0)     # no value-derivation request: recipes did the work
    assert st["run"]["llm_requests"] == 1
    ps = _profiles(client, run_id)
    assert [p["years_of_experience"] for p in ps] == [8, 12, 3]
    assert ps[2]["specialities"] == ["Luxury", "Condo"]
    dec = {d["path"]: d for d in client.get("/api/field-decisions").json()}
    assert dec["years"]["decisions"][0]["recipe_verified"] is True


def test_unverified_recipe_is_not_trusted(client, monkeypatch):
    stub = StubClient(recipes="bad")
    _use(monkeypatch, stub)
    run_id, st = _run(client, _upload(client, THREE, "site")["id"])
    assert st["run"]["status"] == "completed", st
    assert stub.norm_calls == 1                              # a wrong recipe is ignored; Claude derives the values instead
    assert [p["years_of_experience"] for p in _profiles(client, run_id)] == [8, 12, 3]
    dec = {d["path"]: d for d in client.get("/api/field-decisions").json()}
    assert dec["years"]["decisions"][0]["recipe_verified"] is False


# ---- no-LLM runs -----------------------------------------------------------

def test_no_llm_run_reuses_decisions_and_recipes(client, monkeypatch):
    stub = StubClient(recipes="good")
    _use(monkeypatch, stub)
    up = _upload(client, THREE, "site")["id"]
    _run(client, up)
    calls = stub.total
    monkeypatch.delenv("ANTHROPIC_API_KEY")                  # a no-LLM run must not even need a key
    r = client.post("/api/runs", json={"upload_id": up, "strict_no_llm": True})
    assert r.status_code == 200
    from test_pipeline import _wait
    st = _wait(client, r.json()["run_id"])
    assert st["run"]["status"] == "completed", st
    assert stub.total == calls and st["run"]["llm_requests"] == 0
    assert [p["years_of_experience"] for p in _profiles(client, r.json()["run_id"])] == [8, 12, 3]


def test_no_llm_run_skips_unseen_fields_and_unrecipeable_values(client, monkeypatch):
    stub = StubClient(recipes="none")                         # no recipes: values needed Claude
    _use(monkeypatch, stub)
    _run(client, _upload(client, THREE, "site")["id"])
    calls = stub.total
    newer = [dict(REC("Di Park", "d@x.com", "5 years", "Land"), brand_new_field="x")]
    up2 = _upload(client, newer, "site")["id"]
    run_id, st = _run_strict(client, up2)
    assert st["run"]["status"] == "completed" and stub.total == calls
    p = _profiles(client, run_id)[0]
    assert p["name"] == "Di Park" and p["email1"] == "d@x.com"
    assert "years_of_experience" not in p                     # needs Claude to transform and has no verified recipe
    rows = {m["source_field"]: m for m in client.get(f"/api/runs/{run_id}/mappings").json()["mappings"]}
    assert "no verified recipe" in rows["years"]["validation_note"]
    assert rows["brand_new_field"]["status"] == "unmapped" and "no-LLM" in rows["brand_new_field"]["reason"]


def _run_strict(client, upload_id):
    from test_pipeline import _wait
    rid = client.post("/api/runs", json={"upload_id": upload_id, "strict_no_llm": True}).json()["run_id"]
    return rid, _wait(client, rid)


def test_no_llm_run_needs_per_field_mode(client):
    r = client.post("/api/runs", json={"upload_id": _upload(client, THREE)["id"], "mapping_mode": "per_record", "strict_no_llm": True})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_MODE"


# ---- streaming ingest ------------------------------------------------------

def _post(client, name, body):
    return client.post("/api/upload", files={"file": (name, body)})


@pytest.mark.parametrize("name,render", [
    ("d.json", lambda recs: json.dumps(recs, indent=1).encode()),
    ("d.jsonl", lambda recs: "\n".join(json.dumps(r) for r in recs).encode()),
])
def test_streaming_formats_survive_tiny_read_chunks(client, monkeypatch, name, render):
    from app.services import parser
    monkeypatch.setattr(parser, "CHUNK", 37)  # force records to straddle read boundaries
    recs = [{"agent_name": f"Agent {i}", "phone": str(i), "note": "café, \"quoted\" {braces} [x]"} for i in range(200)]
    r = _post(client, name, render(recs))
    assert r.status_code == 200, r.text
    up = r.json()
    assert up["num_records"] == 200 and up["num_fields"] == 3 and up["records_shown"] == 50
    assert up["records"][49]["note"] == recs[49]["note"] and up["format"] == ("JSONL" if name.endswith("l") else "JSON")
    from app.services import database as db
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM upload_records WHERE upload_id=?", (up["id"],)).fetchone()[0] == 200


def test_streaming_errors_leave_no_partial_upload(client):
    from app.services import database as db
    bad = _post(client, "x.json", b'[{"a": 1}, {"a": 2}, {"a": ')
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVALID_JSON"
    unclosed = _post(client, "y.json", b'[{"a": 1}, {"a": 2}')
    assert unclosed.status_code == 400 and unclosed.json()["detail"]["code"] == "INVALID_JSON"
    line = _post(client, "z.jsonl", b'{"a": 1}\n{"a": \n{"a": 3}\n')
    assert line.status_code == 400 and "line 2" in line.json()["detail"]["message"]
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM upload_records").fetchone()[0] == 0


# ---- batched apply ---------------------------------------------------------

def test_batches_give_the_same_result_as_one_batch(client, monkeypatch):
    from app import config
    recs = [REC(f"Agent {i}", f"a{i}@x.com", f"{i + 1} years", "A, B") for i in range(11)]
    outputs = []
    for batch, ingest in ((1000, 1000), (3, 2)):
        monkeypatch.setattr(config, "APPLY_BATCH_SIZE", batch)
        monkeypatch.setattr(config, "INGEST_BATCH_SIZE", ingest)
        _use(monkeypatch, StubClient(recipes="good"))
        run_id, st = _run(client, _upload(client, recs, f"src-{batch}")["id"])
        assert st["run"]["status"] == "completed", st
        cov = client.get(f"/api/runs/{run_id}/coverage").json()["metrics"]
        outputs.append((_profiles(client, run_id), cov["populated_fields"], cov["profile_completeness"], st["run"]["fields_mapped"]))
        assert client.get("/api/db/field_mappings", params={"run_id": run_id}).json()["counts"]["canonical_records"] >= 11
    assert outputs[0] == outputs[1]


def test_large_runs_reference_the_upload_instead_of_copying_raw(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "RAW_COPY_LIMIT", 2)
    _use(monkeypatch, StubClient(recipes="good"))
    run_id, st = _run(client, _upload(client, THREE, "site")["id"])
    assert st["run"]["status"] == "completed"
    rows = client.get("/api/db/source_records", params={"run_id": run_id}).json()["rows"]
    assert all(r["raw_json"] == "" for r in rows)
    rec = client.get(f"/api/runs/{run_id}/canonical").json()["records"][1]
    assert rec["raw"]["agent_name"] == "Bob Jones"           # raw is still reachable, from the stored upload


def test_failure_in_a_later_batch_removes_earlier_batches(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "APPLY_BATCH_SIZE", 2)
    _use(monkeypatch, StubClient("bad_norm_second"))          # no recipes, so each batch needs Claude; the 2nd batch is invalid
    run_id, st = _run(client, _upload(client, THREE + THREE, "site")["id"])
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "MALFORMED_LLM_RESPONSE"
    counts = client.get("/api/db/mapping_runs").json()["counts"]
    assert counts["field_mappings"] == 0 and counts["canonical_records"] == 0 and counts["source_records"] == 6


def test_mode_limits_protect_memory(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "SMALL_MODE_LIMIT", 2)
    _use(monkeypatch, StubClient())
    run_id, st = _run(client, _upload(client, THREE)["id"], "per_record")
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "MODE_TOO_LARGE"


# ---- paging and grouped review ---------------------------------------------

def test_paging_review_queue_and_review_all(client, monkeypatch):
    _use(monkeypatch, StubClient())
    recs = [{"agent_name": f"Agent {i}", "contact": f"Person {i}"} for i in range(5)]
    run_id, st = _run(client, _upload(client, recs, "site")["id"])
    page = client.get(f"/api/runs/{run_id}/mappings", params={"limit": 4, "offset": 4}).json()
    assert page["total"] == 10 and len(page["mappings"]) == 4
    only = client.get(f"/api/runs/{run_id}/mappings", params={"record": 2, "status": "ambiguous"}).json()
    assert only["total"] == 1 and only["mappings"][0]["record_index"] == 2
    q = client.get(f"/api/runs/{run_id}/review-queue").json()
    assert q["total_decisions"] == 1 and q["items"][0]["affects"] == 5
    pg = client.get(f"/api/runs/{run_id}/canonical", params={"limit": 2, "offset": 3}).json()
    assert pg["total"] == 5 and [r["record_index"] for r in pg["records"]] == [3, 4]
    r = client.post(f"/api/mappings/{q['items'][0]['id']}/review", json={"action": "approve", "target_field": "company_name", "scope": "all"})
    assert r.status_code == 200 and r.json()["updated"] == 5
    assert [p["company"][0]["company_name"] for p in _profiles(client, run_id)] == [f"Person {i}" for i in range(5)]
    assert client.get(f"/api/runs/{run_id}/review-queue").json()["total_decisions"] == 0
    assert client.get(f"/api/runs/{run_id}/status").json()["run"]["fields_mapped"] == 10


def test_coverage_is_cached_and_refreshed_by_review(client, monkeypatch):
    _use(monkeypatch, StubClient())
    recs = [{"agent_name": "Al", "contact": "John"}, {"agent_name": "Bo", "contact": "Jane"}]
    run_id, _ = _run(client, _upload(client, recs, "site")["id"])
    before = client.get(f"/api/runs/{run_id}/coverage").json()["metrics"]
    assert before["populated_fields"] == 1
    from app.services import database as db
    with db.conn() as c:
        assert c.execute("SELECT coverage_json IS NOT NULL FROM mapping_runs WHERE id=?", (run_id,)).fetchone()[0] == 1
    q = client.get(f"/api/runs/{run_id}/review-queue").json()["items"][0]
    client.post(f"/api/mappings/{q['id']}/review", json={"action": "approve", "target_field": "company_name", "scope": "all"})
    assert client.get(f"/api/runs/{run_id}/coverage").json()["metrics"]["populated_fields"] == 2


def test_no_llm_run_reuses_decisions_made_under_an_older_prompt_version(client, monkeypatch):
    from app import config
    stub = StubClient(recipes="good")
    _use(monkeypatch, stub)
    up = _upload(client, THREE, "site")["id"]
    monkeypatch.setattr(config, "PROMPT_VERSION", "3.0")
    _run(client, up)
    monkeypatch.setattr(config, "PROMPT_VERSION", "4.0")             # the prompt changed since the source was learned
    calls = stub.total
    rid, st = _run_strict(client, up)
    assert st["run"]["status"] == "completed" and stub.total == calls
    assert [p["years_of_experience"] for p in _profiles(client, rid)] == [8, 12, 3]
    assert "prompt version(s) 3.0" in next(x for x in st["steps"] if x["step_key"] == "send_to_claude")["detail"]
    _run(client, up)                                                   # a normal run, however, re-learns after a prompt change
    assert stub.field_calls == 2


def test_no_llm_run_without_saved_decisions_fails_clearly(client, monkeypatch):
    _use(monkeypatch, StubClient())
    _run(client, _upload(client, THREE, "site-a")["id"])
    rid, st = _run_strict(client, _upload(client, THREE, "a-different-source")["id"])
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "NO_SAVED_DECISIONS"
    assert "'site-a'" in st["run"]["error_message"] and "a-different-source" in st["run"]["error_message"]
    assert client.get("/api/db/mapping_runs").json()["counts"]["canonical_records"] == 3   # only the first run wrote profiles


def test_saved_decision_for_a_field_removed_from_the_schema_is_not_applied(client, monkeypatch):
    _use(monkeypatch, StubClient(recipes="good"))
    up = _upload(client, THREE, "site")["id"]
    _run(client, up)
    from app.services import database as db
    with db.conn() as c:   # simulate a schema edit: the decision for 'phone' now points at a field that no longer exists
        d = json.loads(c.execute("SELECT decisions_json FROM field_decisions WHERE path='phone'").fetchone()[0])
        d[0]["target_field"] = "removed_field"
        c.execute("UPDATE field_decisions SET decisions_json=? WHERE path='phone'", (json.dumps(d),))
    rid, st = _run_strict(client, up)
    assert st["run"]["status"] == "completed"
    m = {x["source_field"]: x for x in client.get(f"/api/runs/{rid}/mappings").json()["mappings"] if x["record_index"] == 0}
    assert m["phone"]["status"] == "unmapped" and "no longer in the schema" in m["phone"]["reason"]
    assert _profiles(client, rid)[0]["name"] == "Alice Smith"
