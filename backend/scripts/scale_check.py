"""Scale check: stream N synthetic records through ingest -> per_field run -> no-LLM re-run, and report time, memory and size.

*** Claude is replaced by a tiny STUB here, so this measures the pipeline (ingest, batching, SQL, memory), not mapping quality. ***

Usage:  python scripts/scale_check.py 20000 [--keep]
The records are the 30 sample records in sample_data/real_sample_data.json repeated with varied emails, so the number of
distinct fields stays at about 675, like a real source whose pages share a template.
"""
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["ANTHROPIC_API_KEY"] = "stub"

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
workdir = Path(tempfile.mkdtemp(prefix="scale_check_"))
os.environ["DB_PATH"] = str(workdir / "scale.db")

from app import config  # noqa: E402
from app.services import llm_mapper  # noqa: E402

RULES = [("email", "email1"), ("mail", "email1"), ("phone", "phone"), ("mobile", "phone"), ("cell", "phone"),
         ("fullname", "name"), ("full_name", "name"), ("person", "name"), ("agent_name", "name"), ("city", "city"), ("town", "city"),
         ("locality", "city")]
calls = {"n": 0}


def fake_create(**kw):
    calls["n"] += 1
    content = kw["messages"][0]["content"]
    user = content if isinstance(content, str) else "\n\n".join(b["text"] for b in content)
    if "FIELDS TO MAP" in user:
        fields, _ = json.JSONDecoder().raw_decode(user.split("sample_values[0].\n\n", 1)[1])
        items = {f["source_field"]: f["sample_values"][0] for f in fields}
    else:
        raise RuntimeError("scale check stub only answers field-decision requests")
    out = {"schema_version": "2.0", "mappings": [], "unmapped_fields": [], "ambiguous_fields": []}
    for path, v in items.items():
        tail = path.lower().split(".")[-1]
        target = next((t for k, t in RULES if k in tail), None)
        base = {"source_field": path, "confidence": 0.95, "reason": "stub", "recipe": None}
        if target and "@" in v or target and target != "email1":
            out["mappings"].append({**base, "target_field": target, "target_value": None, "status": "mapped"})
        else:
            out["unmapped_fields"].append({**base, "target_field": None, "target_value": None, "status": "unmapped"})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out))], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


llm_mapper.make_client = lambda: SimpleNamespace(messages=SimpleNamespace(create=fake_create))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)  # bytes on macOS


def vary(o, i):
    if isinstance(o, dict):
        return {k: vary(v, i) for k, v in o.items()}
    if isinstance(o, list):
        return [vary(x, i) for x in o]
    if isinstance(o, str) and "@" in o and " " not in o:
        return f"{i}.{o}"
    return o


templates = json.loads(config.SAMPLE_DATA_PATH.read_text())
path = workdir / f"scale_{N}.jsonl"
t0 = time.time()
with open(path, "w") as f:
    for i in range(N):
        f.write(json.dumps(vary(templates[i % len(templates)], i)) + "\n")
print(f"generated {N:,} records -> {path.stat().st_size / 1e6:,.0f} MB in {time.time() - t0:.0f}s")

with TestClient(app) as c:
    base = rss_mb()
    t0 = time.time()
    from app.api.upload import _ingest  # called directly: the HTTP test client would load the whole file into memory itself
    with open(path, "rb") as fh:
        up = _ingest(path.name, fh, None, None, "scale-source")
    print(f"ingest:    {time.time() - t0:6.1f}s  records={up['num_records']:,}  distinct fields={up['num_fields']:,}  peak RSS {rss_mb():.0f} MB (start {base:.0f})")

    def run(strict=False):
        t = time.time()
        before = calls["n"]
        rid = c.post("/api/runs", json={"upload_id": up["id"], "strict_no_llm": strict}).json()["run_id"]
        while True:
            st = c.get(f"/api/runs/{rid}/status").json()
            if st["run"]["status"] != "running":
                return rid, st, time.time() - t, calls["n"] - before
            time.sleep(0.5)

    for label, strict in (("first run", False), ("re-run, no LLM", True)):
        rid, st, secs, nreq = run(strict)
        r = st["run"]
        print(f"{label:<15}{secs:6.1f}s  status={r['status']}  claude requests={nreq}  mapped/unmapped source fields={r['fields_mapped']:,}/{r['fields_unmapped']:,}  peak RSS {rss_mb():.0f} MB")
        if r["status"] != "completed":
            print("   ", r["error_type"], r["error_message"])
            break
        t = time.time()
        cov = c.get(f"/api/runs/{rid}/coverage").json()
        print(f"   coverage endpoint {time.time() - t:5.2f}s  populated fields={cov['metrics']['populated_fields']}  completeness={cov['metrics']['profile_completeness']:.1%}")
        t = time.time()
        page = c.get(f"/api/runs/{rid}/mappings", params={"limit": 300, "offset": 1000}).json()
        print(f"   one page of mappings {time.time() - t:5.2f}s  (total rows {page['total']:,})")

print(f"database file: {(workdir / 'scale.db').stat().st_size / 1e6:,.0f} MB   work dir: {workdir}")
if "--keep" not in sys.argv:
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
