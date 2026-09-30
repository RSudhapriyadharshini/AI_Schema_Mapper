"""Cost-saving mapping modes, flattening, placeholders, list fields, and reuse of saved decisions."""
import json

from test_pipeline import RULES, StubClient, _run, _upload, client  # noqa: F401  (client is a fixture)

REC = lambda n, e, y: {"agent_name": n, "phone": "555", "email": e, "years": y}  # noqa: E731
THREE = [REC("Alice Smith", "a@x.com", "8 years"), REC("Bob Jones", "b@x.com", "12+ years"), REC("Cy Lee", "c@x.com", "3 years")]


def _use(monkeypatch, stub):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)


def test_per_field_decides_each_field_once(client, monkeypatch):
    stub = StubClient()
    _use(monkeypatch, stub)
    run_id, st = _run(client, _upload(client, THREE)["id"])
    assert st["run"]["status"] == "completed", st
    # 4 distinct fields -> 1 field-decision request; years needs deriving -> 1 batched value request
    assert (stub.field_calls, stub.norm_calls, stub.record_calls) == (1, 1, 0)
    assert st["run"]["llm_requests"] == 2 and st["run"]["fields_mapped"] == 12
    canon = client.get(f"/api/runs/{run_id}/canonical").json()["records"]
    assert [c["profile"]["years_of_experience"] for c in canon] == [8, 12, 3]
    assert canon[1]["profile"]["email1"] == "b@x.com"
    origins = {r["origin"] for r in client.get("/api/db/field_mappings", params={"run_id": run_id}).json()["rows"]}
    assert origins == {"reused"}  # every record used a per-field decision


def test_per_field_reuse_across_runs_and_sources(client, monkeypatch):
    stub = StubClient()
    _use(monkeypatch, stub)
    up = _upload(client, THREE, "site-a")["id"]
    _run(client, up)
    run2, st2 = _run(client, up)
    assert stub.field_calls == 1                 # second run: no field-decision request at all
    assert st2["run"]["llm_requests"] == 1       # only the batched value derivation
    saved = client.get("/api/field-decisions").json()
    assert {s["source_name"] for s in saved} == {"site-a"} and all(s["times_reused"] == 1 for s in saved)
    assert client.delete("/api/field-decisions", params={"source_name": "site-a"}).status_code == 200
    assert client.get("/api/field-decisions").json() == []
    _run(client, up)  # re-learn after forgetting
    assert stub.field_calls == 2                 # forgotten decisions are asked about again
    _run(client, up, "per_field_relearn")
    assert stub.field_calls == 3                 # relearn ignores saved decisions
    _run(client, _upload(client, THREE, "site-b")["id"])
    assert stub.field_calls == 4                 # a different source is decided by Claude again, never guessed from site-a


def test_per_field_chunks_requests(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "FIELD_CHUNK", 2)
    stub = StubClient()
    _use(monkeypatch, stub)
    _run(client, _upload(client, THREE)["id"])
    assert stub.field_calls == 2  # 4 fields, 2 per request


def test_per_source_and_per_record_baselines(client, monkeypatch):
    stub = StubClient()
    _use(monkeypatch, stub)
    up = _upload(client, THREE)["id"]
    _, st = _run(client, up, "per_source")
    assert (stub.record_calls, stub.norm_calls) == (1, 1) and st["run"]["llm_requests"] == 2
    _run(client, up, "per_source")
    assert stub.record_calls == 1                # saved source mapping reused
    _run(client, up, "per_source_relearn")
    assert stub.record_calls == 2
    before = stub.record_calls
    _, st = _run(client, up, "per_record")
    assert stub.record_calls - before == 3 and st["run"]["status"] == "completed"


def test_nested_paths_placeholders_and_lists(client, monkeypatch):
    stub = StubClient()
    _use(monkeypatch, stub)
    recs = [{"agent_name": "Al", "fax": "N/A", "phone": "-", "loc": {"city": "Austin", "zip": ""}, "skills": "Buyer, Seller , Buyer",
             "brokerage": "ABC Realty"}]
    up = _upload(client, recs)
    assert {f["field"] for f in up["fields"]} == {"agent_name", "loc.city", "skills", "brokerage"}  # flattened, junk dropped
    assert up["dropped_empty"] == 3
    assert up["records"][0]["fax"] == "N/A"  # raw record is stored untouched
    run_id, st = _run(client, up["id"])
    assert st["run"]["status"] == "completed", st
    p = client.get(f"/api/runs/{run_id}/canonical").json()["records"][0]["profile"]
    assert p["city"] == "Austin" and p["specialities"] == ["Buyer", "Seller"]
    assert p["company"] == [{"company_name": "ABC Realty"}] and "fax" not in p
    raw = client.get("/api/db/source_records", params={"run_id": run_id}).json()["rows"][0]["raw_json"]
    assert json.loads(raw)["fax"] == "N/A"


def test_list_fields_merge_and_copy(client, monkeypatch):
    stub = StubClient()
    _use(monkeypatch, stub)
    recs = [{"agent_name": "Al", "skills": ["A", "B"]}, {"agent_name": "Bo", "skills": ["C"]}]
    run_id, st = _run(client, _upload(client, recs)["id"])
    assert st["run"]["status"] == "completed"
    assert stub.norm_calls == 0  # a list that already fits the target type is copied, not re-derived
    ps = [r["profile"] for r in client.get(f"/api/runs/{run_id}/canonical").json()["records"]]
    assert ps[0]["specialities"] == ["A", "B"] and ps[1]["specialities"] == ["C"]


def test_review_decision_is_reused_next_run(client, monkeypatch):
    for mode in ("per_field", "per_source"):
        stub = StubClient()
        _use(monkeypatch, stub)
        recs = [{"agent_name": "Al One", "contact": "John Smith"}, {"agent_name": "Bo Two", "contact": "Jane Doe"}]
        up = _upload(client, recs, f"src-{mode}")["id"]
        run1, _ = _run(client, up, mode)
        amb = [m for m in client.get(f"/api/runs/{run1}/mappings").json()["mappings"] if m["source_field"] == "contact" and m["record_index"] == 0][0]
        assert amb["status"] == "ambiguous"
        assert client.post(f"/api/mappings/{amb['id']}/review", json={"action": "approve", "target_field": "company_name"}).status_code == 200
        calls = stub.total
        run2, st2 = _run(client, up, mode)
        assert stub.total == calls, mode  # the human-corrected decision is reused, Claude is not asked again
        canon = client.get(f"/api/runs/{run2}/canonical").json()["records"]
        assert [c["profile"]["company"][0]["company_name"] for c in canon] == ["John Smith", "Jane Doe"], mode


def test_bad_normalization_fails_run_and_writes_nothing(client, monkeypatch):
    _use(monkeypatch, StubClient("bad_norm"))
    run_id, st = _run(client, _upload(client, THREE)["id"])
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "MALFORMED_LLM_RESPONSE"
    counts = client.get("/api/db/mapping_runs").json()["counts"]
    assert counts["field_mappings"] == 0 and counts["canonical_records"] == 0


def test_type_failure_is_demoted_not_written(client, monkeypatch):
    _use(monkeypatch, StubClient())
    recs = [{"agent_name": "Al", "email": "not-an-email"}]
    run_id, st = _run(client, _upload(client, recs)["id"])
    rows = client.get(f"/api/runs/{run_id}/mappings").json()["mappings"]
    em = [r for r in rows if r["source_field"] == "email"][0]
    assert em["status"] == "ambiguous" and "valid email" in em["validation_note"]
    assert "email1" not in client.get(f"/api/runs/{run_id}/canonical").json()["records"][0]["profile"]
