"""Map-once-per-source: Claude decides once per field-name signature; later records reuse the decision.

Uses the same STUB Claude client idea as test_pipeline.py (test-only scaffolding).
"""
import json
from types import SimpleNamespace

from test_pipeline import StubClient, _upload, _wait, client  # noqa: F401  (client is a fixture)

REC = lambda n, e: {"agent_name": n, "phone": "555", "email": e}  # noqa: E731
THREE = [REC("Alice Smith", "a@x.com"), REC("Bob Jones", "b@x.com"), REC("Cy Lee", "c@x.com")]


class SourceStub(StubClient):
    """Answers mapping requests like StubClient and normalization requests by splitting full names."""

    def __init__(self, bad_norm=False):
        super().__init__()
        self.map_calls = self.norm_calls = 0
        self.bad_norm = bad_norm
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        if "ALREADY been decided" in kw["system"]:
            self.norm_calls += 1
            req = json.loads(kw["messages"][0]["content"])
            results = []
            for rec in req["records"]:
                for m in req["mappings_to_apply"]:
                    first, _, last = str(rec["values"][m["source_field"]]).partition(" ")
                    results.append({"record_index": rec["record_index"], "source_field": m["source_field"],
                                    "target_field": m["target_field"], "target_value": first if m["target_field"].endswith("first_name") else last})
            if self.bad_norm:
                results = results[:1]
            text = json.dumps({"results": results})
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=5, output_tokens=5))
        self.map_calls += 1
        return super().create(**kw)


def _run(client, upload_id, mode="per_source"):
    run_id = client.post("/api/runs", json={"upload_id": upload_id, "mapping_mode": mode}).json()["run_id"]
    return run_id, _wait(client, run_id)


def _origins(client, run_id):
    rows = client.get("/api/db/field_mappings", params={"run_id": run_id}).json()["rows"]
    return sorted({(r["record_id"], r["origin"]) for r in rows})


def test_reuse_within_run(client, monkeypatch):
    from app.services import llm_mapper
    stub = SourceStub()
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)
    run_id, st = _run(client, _upload(client, THREE)["id"])
    assert st["run"]["status"] == "completed", st
    assert (stub.map_calls, stub.norm_calls) == (1, 1)  # 3 records, 2 Claude requests
    assert st["run"]["llm_requests"] == 2 and st["run"]["fields_mapped"] == 9
    canon = client.get(f"/api/runs/{run_id}/canonical").json()["records"]
    assert [c["profile"]["first_name"] for c in canon] == ["Alice", "Bob", "Cy"]
    assert canon[2]["profile"]["last_name"] == "Lee" and canon[2]["profile"]["phone"] == "555"
    assert len({o for _, o in _origins(client, run_id)}) == 2  # llm + reused
    assert len(client.get("/api/source-mappings").json()) == 1


def test_reuse_across_runs_and_relearn(client, monkeypatch):
    from app.services import llm_mapper
    stub = SourceStub()
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)
    up = _upload(client, THREE)["id"]
    _run(client, up)
    run2, st2 = _run(client, up)
    assert stub.map_calls == 1                      # second run needed no mapping request at all
    assert st2["run"]["llm_requests"] == 1          # only the small value-derivation request
    assert {o for _, o in _origins(client, run2)} == {"reused"}
    assert client.get("/api/source-mappings").json()[0]["times_reused"] == 1
    _run(client, up, "per_source_relearn")
    assert stub.map_calls == 2                      # relearn ignores the saved mapping


def test_per_record_baseline_maps_every_record(client, monkeypatch):
    from app.services import llm_mapper
    stub = SourceStub()
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)
    run_id, st = _run(client, _upload(client, THREE)["id"], "per_record")
    assert st["run"]["status"] == "completed"
    assert (stub.map_calls, stub.norm_calls) == (3, 0)
    assert client.get("/api/source-mappings").json() == []  # baseline mode saves nothing


def test_different_field_names_are_different_sources(client, monkeypatch):
    from app.services import llm_mapper
    stub = SourceStub()
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)
    recs = [REC("Al One", "a@x.com"), {"agent_name": "Bo Two", "phone": "5", "email": "b@x.com", "favorite_color": "teal"}]
    run_id, st = _run(client, _upload(client, recs)["id"])
    assert st["run"]["status"] == "completed" and stub.map_calls == 2


def test_review_decision_is_reused_next_run(client, monkeypatch):
    from app.services import llm_mapper
    stub = SourceStub()
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)
    recs = [{"agent_name": "Al One", "contact": "John Smith"}, {"agent_name": "Bo Two", "contact": "Jane Doe"}]
    up = _upload(client, recs)["id"]
    run1, _ = _run(client, up)
    amb = [m for m in client.get(f"/api/runs/{run1}/mappings").json()["mappings"] if m["source_field"] == "contact" and m["record_index"] == 0][0]
    assert amb["status"] == "ambiguous"
    assert client.post(f"/api/mappings/{amb['id']}/review", json={"action": "approve", "target_field": "profile.company"}).status_code == 200
    calls = stub.map_calls
    run2, st2 = _run(client, up)
    assert stub.map_calls == calls  # saved (now human-corrected) mapping reused
    canon = client.get(f"/api/runs/{run2}/canonical").json()["records"]
    assert [c["profile"]["company"] for c in canon] == ["John Smith", "Jane Doe"]


def test_bad_normalization_fails_run_and_writes_nothing(client, monkeypatch):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: SourceStub(bad_norm=True))
    run_id, st = _run(client, _upload(client, THREE)["id"])
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "MALFORMED_LLM_RESPONSE"
    counts = client.get("/api/db/mapping_runs").json()["counts"]
    assert counts["field_mappings"] == 0 and counts["canonical_records"] == 0


def test_invalid_mode_rejected(client, monkeypatch):
    up = _upload(client, THREE)["id"]
    r = client.post("/api/runs", json={"upload_id": up, "mapping_mode": "bogus"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_MODE"
