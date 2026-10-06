"""Slim responses, prompt-cache layout, and early dropping of crawler metadata blocks (STUB Claude, test-only)."""
import json
from types import SimpleNamespace

from test_pipeline import StubClient, _run, _text, _upload, client  # noqa: F401  (client is a fixture)


def _use(monkeypatch, stub):
    from app.services import llm_mapper
    monkeypatch.setattr(llm_mapper, "make_client", lambda: stub)


class ChattyStub(StubClient):
    """Returns the old verbose fields (wrong owner, wrong etl flag, wrong source_value): the app must ignore them."""

    def create(self, **kw):
        resp = super().create(**kw)
        payload = json.loads(resp.content[0].text)
        for lst in ("mappings", "ambiguous_fields", "unmapped_fields"):
            for it in payload.get(lst, []):
                it.update({"owner": "SYSTEM", "etl_can_populate": True, "source_value": "WRONG"})
        resp.content[0].text = json.dumps(payload)
        return resp


class NullValueStub(StubClient):
    """Says nothing about target_value for specialities, although the text must become a list."""

    def create(self, **kw):
        resp = super().create(**kw)
        payload = json.loads(resp.content[0].text)
        for it in payload.get("mappings", []):
            if it["target_field"] == "specialities":
                it["target_value"] = None
        resp.content[0].text = json.dumps(payload)
        return resp


def _rows(client, run_id):
    return {(m["source_field"], m["target_field"]): m for m in client.get(f"/api/runs/{run_id}/mappings").json()["mappings"]}


def test_null_target_value_means_copy(client, monkeypatch):
    _use(monkeypatch, StubClient())
    run_id, st = _run(client, _upload(client, [{"agent_name": "  Alice Smith "}], "s")["id"])
    m = _rows(client, run_id)[("agent_name", "name")]
    assert m["status"] == "mapped" and m["target_value"] == "Alice Smith" and m["source_value"] == "Alice Smith"   # whitespace trimmed
    assert client.get("/api/field-decisions").json()[0]["decisions"][0]["derived"] is False


def test_owner_etl_flag_and_source_value_come_from_schema_and_raw_data(client, monkeypatch):
    _use(monkeypatch, ChattyStub())
    run_id, st = _run(client, _upload(client, [{"agent_name": "Alice", "lic": "TX-1"}], "s")["id"])
    rows = _rows(client, run_id)
    name, lic = rows[("agent_name", "name")], rows[("lic", "licenses")]
    assert name["owner"] == "ETL" and name["etl_can_populate"] is True and name["source_value"] == "Alice"
    assert lic["owner"] == "OTHER_SOURCE" and lic["etl_can_populate"] is False   # the crawler is not an accepted source for licenses


def test_null_target_value_on_a_field_that_needs_reshaping_falls_back_to_claude_values(client, monkeypatch):
    stub = NullValueStub()
    _use(monkeypatch, stub)
    run_id, st = _run(client, _upload(client, [{"agent_name": "Al", "skills": "Buyer, Seller"}], "s")["id"])
    assert st["run"]["status"] == "completed" and stub.norm_calls == 1     # the raw text is not a list, so Claude is asked for the value
    p = client.get(f"/api/runs/{run_id}/canonical").json()["records"][0]["profile"]
    assert p["specialities"] == ["Buyer", "Seller"]


def test_same_case_in_a_no_llm_run_is_skipped_not_guessed(client, monkeypatch):
    _use(monkeypatch, NullValueStub())
    up = _upload(client, [{"agent_name": "Al", "skills": "Buyer, Seller"}], "s")["id"]
    _run(client, up)
    from test_pipeline import _wait
    rid = client.post("/api/runs", json={"upload_id": up, "strict_no_llm": True}).json()["run_id"]
    _wait(client, rid)
    assert "specialities" not in client.get(f"/api/runs/{rid}/canonical").json()["records"][0]["profile"]


def test_prompt_layout_is_cache_friendly(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "FIELD_CHUNK", 2)
    monkeypatch.setattr(config, "MAPPER_CONCURRENCY", 1)
    stub = StubClient()
    _use(monkeypatch, stub)
    recs = [{"agent_name": "Al", "phone": "1", "email": "a@x.com", "years": "5 years", "skills": "A, B"}]
    run_id, st = _run(client, _upload(client, recs, "s")["id"])
    assert st["run"]["status"] == "completed", st
    mapping = [kw for kw in stub.requests if not isinstance(kw["messages"][0]["content"], str)]
    assert len(mapping) == 3                                       # 5 fields, 2 per request
    firsts = [kw["messages"][0]["content"][0] for kw in mapping]
    assert all(b["cache_control"] == {"type": "ephemeral"} for b in firsts)
    assert len({b["text"] for b in firsts}) == 1 and len({kw["system"] for kw in mapping}) == 1   # byte-identical prefix
    assert len({kw["messages"][0]["content"][1]["text"] for kw in mapping}) == 3               # the part that varies comes after it
    assert "CANONICAL SCHEMA" in firsts[0]["text"] and "FIELDS TO MAP" not in firsts[0]["text"]
    assert all(isinstance(kw["messages"][0]["content"], str) for kw in stub.requests if "ALREADY been decided" in kw["system"])
    run = st["run"]
    assert run["cache_write_tokens"] == 4000 and run["cache_read_tokens"] == 8000              # 1 write, then 2 reads
    calls = client.get(f"/api/runs/{run_id}/logs").json()["calls"]
    assert [c["cache_read_tokens"] for c in calls if c["purpose"] == "map_fields"] == [0, 4000, 4000]


def test_metadata_blocks_are_dropped_early_but_kept_in_raw(client, monkeypatch):
    from app import config
    _use(monkeypatch, StubClient())
    recs = [{"agent_name": f"Agent {i}", "crawled_from": "https://site.example/a", "crawler_meta": {"row": i, "crawl_id": f"id-{i}", "ts": 1}}
            for i in range(3)]
    up = _upload(client, recs, "s")
    assert {f["field"] for f in up["fields"]} == {"agent_name", "crawled_from"}               # crawled_from is a real field, kept
    assert up["metadata_ignored"] == 9 and up["metadata_blocks"] == {"crawler_meta": 3}
    assert up["records"][0]["crawler_meta"]["crawl_id"] == "id-0"                              # raw record keeps everything
    run_id, st = _run(client, up["id"])
    assert {k[0] for k in _rows(client, run_id)} == {"agent_name", "crawled_from"}
    monkeypatch.setattr(config, "DROP_METADATA_BLOCKS", False)
    up2 = _upload(client, recs, "s2")
    assert up2["metadata_ignored"] == 0 and any(f["field"].startswith("crawler_meta.") for f in up2["fields"])


def test_block_name_rules():
    from app.services.parser import is_metadata_block as m
    assert all(m(k) for k in ["crawler_meta", "_meta", "extraction-meta", "pipeline_info", "_crawl_info", "extractionMeta", "run_info"])
    assert not any(m(k) for k in ["crawled_from", "web_presence", "company_data", "contact_info", "source_info", "social_links"])
