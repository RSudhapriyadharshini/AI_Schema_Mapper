"""End-to-end tests of upload -> run -> validation -> DB using a STUB Claude client.

The stub is test-only scaffolding so the pipeline, validation, retry, coverage math and DB writes can be
exercised offline. The application itself never contains canned mappings. These tests do NOT measure how
well the real model maps fields.
"""
import json
import os
import re
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


# stub "model" knowledge, keyed by source path (test only)
RULES = {
    "agent_name": ["name"], "phone": ["phone"], "email": ["email1"], "brokerage": ["company_name"],
    "years": ["years_of_experience"], "skills": ["specialities"], "loc.city": ["city"], "fax": ["fax"], "lic": ["licenses"],
}
AMBIGUOUS = {"contact"}


def _derive(value, target):
    if target == "years_of_experience":
        return re.match(r"\d+", str(value)).group(0)
    if target == "licenses":
        return json.dumps([str(value)])
    if target == "specialities":
        try:
            lst = json.loads(value)
            if isinstance(lst, list):
                return json.dumps(lst)
        except (TypeError, ValueError):
            pass
        return json.dumps([x.strip() for x in str(value).split(",")])
    return str(value)


def _item(sf, v, tf, tv, conf, status):
    """A slim answer item: no echoed source_value / owner / etl_can_populate; target_value null when the value is copied."""
    copy = status != "mapped" or str(tv) == str(v)
    return {"source_field": sf, "target_field": tf, "target_value": None if copy else tv, "confidence": conf,
            "status": status, "reason": "stub", "recipe": None}


RECIPES = {  # what a good "model" would return as the recipe for a transformed field (test only)
    "years_of_experience": [{"op": "first_number"}],
    "specialities": [{"op": "split_list", "delimiters": [","]}],
}


def _answer(fields: dict, recipes: str = "none") -> dict:
    out = {"schema_version": "2.0", "mappings": [], "unmapped_fields": [], "ambiguous_fields": []}
    for f, v in fields.items():
        if f in AMBIGUOUS:
            out["ambiguous_fields"].append(_item(f, v, None, None, 0.4, "ambiguous"))
        elif f in RULES:
            for tf in RULES[f]:
                it = _item(f, v, tf, _derive(v, tf), 0.95, "mapped")
                if recipes == "good" and tf in RECIPES:
                    it["recipe"] = json.dumps(RECIPES[tf])
                elif recipes == "bad" and tf in RECIPES:
                    it["recipe"] = json.dumps([{"op": "upper"}])  # does not reproduce the answer
                out["mappings"].append(it)
        else:
            out["unmapped_fields"].append(_item(f, v, None, None, 0.9, "unmapped"))
    return out


def _resp(payload, cache_read=0, cache_write=0):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=20, cache_read_input_tokens=cache_read,
                                                 cache_creation_input_tokens=cache_write))


def _text(content):
    return content if isinstance(content, str) else "\n\n".join(b["text"] for b in content)


class StubClient:
    """Answers the three request kinds the app makes: one record, a list of fields, and value derivation."""

    def __init__(self, mode="ok", recipes="none"):
        self.mode, self.recipes = mode, recipes
        self.record_calls = self.field_calls = self.norm_calls = 0
        self.requests, self._cached_prefixes = [], set()
        self.messages = SimpleNamespace(create=self.create)

    @property
    def total(self):
        return self.record_calls + self.field_calls + self.norm_calls

    def create(self, **kw):
        self.requests.append(kw)
        content = kw["messages"][0]["content"]
        user = _text(content)
        if "ALREADY been decided" in kw["system"]:
            self.norm_calls += 1
            req = json.loads(user)
            results = []
            for rec in req["records"]:
                for m in req["mappings_to_apply"]:
                    if m["source_field"] in rec["values"]:
                        v = rec["values"][m["source_field"]]
                        v = json.dumps(v) if isinstance(v, (list, dict)) else v
                        results.append({"record_index": rec["record_index"], "source_field": m["source_field"],
                                        "target_field": m["target_field"], "target_value": _derive(v, m["target_field"])})
            if self.mode == "bad_norm" or (self.mode == "bad_norm_second" and self.norm_calls >= 2):
                results = results[:1]
            return _resp({"results": results})
        if "FIELDS TO MAP" in user:
            self.field_calls += 1
            after = user.split("sample_values[0].\n\n", 1)[1]
            fields, _ = json.JSONDecoder().raw_decode(after)
            payload = _answer({f["source_field"]: f["sample_values"][0] for f in fields}, self.recipes)
        else:
            self.record_calls += 1
            record = json.loads(user.split("Map every field in it:\n\n", 1)[1])
            payload = _answer(record, self.recipes)
        if self.mode == "bad_field" or (self.mode == "bad_first" and self.total == 1):
            payload["mappings"][0]["target_field"] = "favorite_color"
        # simulate prompt caching: the block marked cache_control is written once, then read
        if isinstance(content, list) and content[0].get("cache_control"):
            key = kw["system"] + content[0]["text"]
            if key in self._cached_prefixes:
                return _resp(payload, cache_read=4000)
            self._cached_prefixes.add(key)
            return _resp(payload, cache_write=4000)
        return _resp(payload)


def _wait(client, run_id):
    for _ in range(150):
        st = client.get(f"/api/runs/{run_id}/status").json()
        if st["run"]["status"] != "running":
            return st
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def _upload(client, records, source_name=""):
    r = client.post("/api/upload", files={"file": ("t.json", json.dumps(records).encode())}, data={"source_name": source_name})
    assert r.status_code == 200, r.text
    return r.json()


def _run(client, upload_id, mode="per_field"):
    run_id = client.post("/api/runs", json={"upload_id": upload_id, "mapping_mode": mode}).json()["run_id"]
    return run_id, _wait(client, run_id)


RECS = [{"agent_name": "Alice Smith", "phone": "+1 555", "email": "a@x.com", "favorite_color": "teal"},
        {"agent_name": "Bob Jones", "phone": "+1 556", "contact": "John Smith"}]


@pytest.mark.parametrize("mode", ["per_field", "per_source", "per_record"])
def test_happy_path_in_every_mode(client, monkeypatch, mode):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: StubClient())
    up = _upload(client, RECS)
    assert (up["num_records"], up["num_fields"], up["fields_extracted"]) == (2, 5, 7)
    run_id, st = _run(client, up["id"], mode)
    assert st["run"]["status"] == "completed", st
    assert all(s["status"] == "done" for s in st["steps"])
    assert (st["run"]["fields_mapped"], st["run"]["fields_ambiguous"], st["run"]["fields_unmapped"]) == (5, 1, 1)
    schema = client.get("/api/schema").json()
    cov = client.get(f"/api/runs/{run_id}/coverage").json()["metrics"]
    assert cov["canonical_fields"] == len(schema["fields"])
    assert cov["populated_fields"] == 3  # name, phone, email1
    assert abs(cov["field_coverage"] - 3 / len(schema["fields"])) < 1e-9
    etl_total = sum(1 for f in schema["fields"] if f["owner"] == "ETL")
    assert cov["etl_owned_total"] == etl_total and cov["etl_owned_populated"] == 3
    canon = client.get(f"/api/runs/{run_id}/canonical").json()["records"][0]
    assert canon["profile"]["name"] == "Alice Smith" and canon["lineage"]["phone"]["source_field"] == "phone"
    assert client.get("/api/db/field_mappings", params={"run_id": run_id}).json()["counts"]["field_mappings"] == 7


def test_review_updates_canonical_and_memory(client, monkeypatch):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: StubClient())
    run_id, _ = _run(client, _upload(client, RECS)["id"])
    amb = [x for x in client.get(f"/api/runs/{run_id}/mappings").json()["mappings"] if x["status"] == "ambiguous"][0]
    r = client.post(f"/api/mappings/{amb['id']}/review", json={"action": "approve", "target_field": "company_name"})
    assert r.status_code == 200
    assert client.get(f"/api/runs/{run_id}/status").json()["run"]["fields_mapped"] == 6
    rec2 = client.get(f"/api/runs/{run_id}/canonical").json()["records"][1]
    assert rec2["profile"]["company"][0]["company_name"] == "John Smith"
    assert client.get("/api/approved-examples").json()[0]["source_field"] == "contact"
    bad = client.post(f"/api/mappings/{amb['id']}/review", json={"action": "approve", "target_field": "profile.nope"})
    assert bad.status_code == 400


def test_retry_after_bad_response(client, monkeypatch):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: StubClient("bad_first"))
    run_id, st = _run(client, _upload(client, RECS[:1])["id"], "per_record")
    assert st["run"]["status"] == "completed"
    assert [c["status"] for c in client.get(f"/api/runs/{run_id}/logs").json()["calls"]] == ["invalid", "ok"]


def test_invalid_field_writes_nothing(client, monkeypatch):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: StubClient("bad_field"))
    run_id, st = _run(client, _upload(client, RECS)["id"])
    assert st["run"]["status"] == "failed" and st["run"]["error_type"] == "INVALID_CANONICAL_FIELD"
    counts = client.get("/api/db/mapping_runs").json()["counts"]
    assert counts["field_mappings"] == 0 and counts["canonical_records"] == 0 and counts["source_records"] == 2


@pytest.mark.parametrize("name,body,code", [
    ("a.json", b"", "EMPTY_FILE"), ("a.json", b"{oops", "INVALID_JSON"), ("a.csv", b"a,b\n1,2,3\n", "INVALID_CSV"),
    ("a.txt", b"x", "UNSUPPORTED_FORMAT"), ("a.json", b"[]", "EMPTY_FILE"), ("a.json", b'[{"a": "N/A", "b": "-"}]', "EMPTY_FILE")])
def test_upload_errors(client, name, body, code):
    r = client.post("/api/upload", files={"file": (name, body)})
    assert r.status_code == 400 and r.json()["detail"]["code"] == code


def test_csv_and_bundled_sample(client):
    r = client.post("/api/upload", files={"file": ("c.csv", b"name,tel\nAl,555\nBo,\n")})
    assert r.json()["format"] == "CSV" and r.json()["fields_extracted"] == 3
    s = client.post("/api/upload/sample").json()
    assert s["file_name"] == "real_sample_data.json" and s["num_records"] == 30
    assert s["num_fields"] > 100 and s["dropped_empty"] > 0  # nested paths flattened, placeholders ignored
    assert any("." in f["field"] for f in s["fields"])


def test_missing_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    r = client.post("/api/runs", json={"upload_id": _upload(client, RECS)["id"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "MISSING_API_KEY"


def test_invalid_mode_rejected(client):
    r = client.post("/api/runs", json={"upload_id": _upload(client, RECS)["id"], "mapping_mode": "bogus"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_MODE"
