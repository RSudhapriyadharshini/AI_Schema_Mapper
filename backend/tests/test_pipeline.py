"""End-to-end tests of upload -> run -> validation -> DB using a STUB Claude client.

The stub is test-only scaffolding so the pipeline, validation, coverage math and DB writes can be
exercised offline. The application itself never contains canned mappings.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest

os.environ["ANTHROPIC_API_KEY"] = "test-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from app.services import database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    database.init_db()
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


LOOKUP = {  # stub "model" answers, keyed by source field name (test only)
    "agent_name": [("profile.full_name", "{v}"), ("profile.first_name", "{first}"), ("profile.last_name", "{last}")],
    "phone": [("profile.phone", "{v}")], "email": [("profile.email", "{v}")],
}


class StubClient:
    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, 0
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls += 1
        user = kw["messages"][0]["content"]
        record = json.loads(user.split("Map every field in it:\n\n", 1)[1])
        items = {"mappings": [], "unmapped_fields": [], "ambiguous_fields": []}
        for f, v in record.items():
            if f == "contact":
                items["ambiguous_fields"].append(_item(f, v, None, None, 0.4, "ambiguous"))
            elif f in LOOKUP:
                for tf, tpl in LOOKUP[f]:
                    first, _, last = str(v).partition(" ")
                    items["mappings"].append(_item(f, v, tf, tpl.format(v=v, first=first, last=last), 0.95, "mapped"))
            else:
                items["unmapped_fields"].append(_item(f, v, None, None, 0.9, "unmapped"))
        if self.mode == "bad_field" or (self.mode == "bad_first" and self.calls == 1):
            items["mappings"][0]["target_field"] = "profile.favorite_color"
        text = json.dumps({"schema_version": "1.0", **items})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=20))


def _item(sf, v, tf, tv, conf, status):
    return {"source_field": sf, "source_value": str(v), "target_field": tf, "target_value": tv, "confidence": conf,
            "status": status, "reason": "stub", "owner": None, "etl_can_populate": status == "mapped"}


def _wait(client, run_id):
    for _ in range(100):
        st = client.get(f"/api/runs/{run_id}/status").json()
        if st["run"]["status"] != "running":
            return st
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def _upload(client, records):
    r = client.post("/api/upload", files={"file": ("t.json", json.dumps(records).encode())})
    assert r.status_code == 200, r.text
    return r.json()


RECS = [{"agent_name": "Alice Smith", "phone": "+1 555", "email": "a@x.com", "favorite_color": "teal"},
        {"agent_name": "Bob Jones", "phone": "+1 556", "contact": "John Smith"}]


def test_happy_path(client, monkeypatch):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: StubClient())
    up = _upload(client, RECS)
    assert (up["num_records"], up["num_fields"], up["fields_extracted"]) == (2, 5, 7)
    run_id = client.post("/api/runs", json={"upload_id": up["id"]}).json()["run_id"]
    st = _wait(client, run_id)
    assert st["run"]["status"] == "completed", st
    assert all(s["status"] == "done" for s in st["steps"])
    assert (st["run"]["fields_mapped"], st["run"]["fields_ambiguous"], st["run"]["fields_unmapped"]) == (5, 1, 1)
    cov = client.get(f"/api/runs/{run_id}/coverage").json()
    m = cov["metrics"]
    assert m["populated_fields"] == 5 and m["canonical_fields"] == 19
    assert abs(m["field_coverage"] - 5 / 19) < 1e-9
    # 13 ETL-owned fields in the schema, 5 populated
    assert m["etl_owned_total"] == 13 and m["etl_owned_populated"] == 5
    canon = client.get(f"/api/runs/{run_id}/canonical").json()["records"][0]
    assert canon["profile"]["first_name"] == "Alice" and canon["lineage"]["phone"]["source_field"] == "phone"
    assert client.get("/api/db/field_mappings", params={"run_id": run_id}).json()["counts"]["field_mappings"] == 11

    # manual review: approve the ambiguous field as an ETL field, then check canonical + memory
    amb = [x for x in client.get(f"/api/runs/{run_id}/mappings").json()["mappings"] if x["status"] == "ambiguous"][0]
    r = client.post(f"/api/mappings/{amb['id']}/review", json={"action": "approve", "target_field": "profile.company"})
    assert r.status_code == 200
    assert client.get(f"/api/runs/{run_id}/status").json()["run"]["fields_mapped"] == 6
    assert client.get(f"/api/runs/{run_id}/canonical").json()["records"][1]["profile"]["company"] == "John Smith"
    assert client.get("/api/approved-examples").json()[0]["source_field"] == "contact"


def test_retry_after_bad_response(client, monkeypatch):
    from app.services import llm_mapper
    stub = StubClient("bad_first")
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)
    up = _upload(client, RECS[:1])
    run_id = client.post("/api/runs", json={"upload_id": up["id"]}).json()["run_id"]
    assert _wait(client, run_id)["run"]["status"] == "completed"
    calls = client.get(f"/api/runs/{run_id}/logs").json()["calls"]
    assert [c["status"] for c in calls] == ["invalid", "ok"]


def test_invalid_field_writes_nothing(client, monkeypatch):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: StubClient("bad_field"))
    up = _upload(client, RECS)
    run_id = client.post("/api/runs", json={"upload_id": up["id"]}).json()["run_id"]
    st = _wait(client, run_id)
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "INVALID_CANONICAL_FIELD"
    counts = client.get("/api/db/mapping_runs").json()["counts"]
    assert counts["field_mappings"] == 0 and counts["canonical_records"] == 0 and counts["source_records"] == 2


@pytest.mark.parametrize("name,body,code", [
    ("a.json", b"", "EMPTY_FILE"), ("a.json", b"{oops", "INVALID_JSON"), ("a.csv", b"a,b\n1,2,3\n", "INVALID_CSV"),
    ("a.txt", b"x", "UNSUPPORTED_FORMAT"), ("a.json", b"[]", "EMPTY_FILE")])
def test_upload_errors(client, name, body, code):
    r = client.post("/api/upload", files={"file": (name, body)})
    assert r.status_code == 400 and r.json()["detail"]["code"] == code


def test_csv_and_sample(client):
    r = client.post("/api/upload", files={"file": ("c.csv", b"name,tel\nAl,555\nBo,\n")})
    assert r.json()["format"] == "CSV" and r.json()["fields_extracted"] == 3
    s = client.post("/api/upload/sample").json()
    assert s["num_records"] >= 5


def test_missing_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    up = _upload(client, RECS)
    r = client.post("/api/runs", json={"upload_id": up["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "MISSING_API_KEY"
