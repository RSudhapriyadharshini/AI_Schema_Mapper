import { useState } from "react";
import { api } from "../services/api";
import { useApp, useAsync } from "../components/AppContext";
import { Button, Card, Empty, ErrorBox, Json, OwnerBadge } from "../components/ui";

const PAGE = 30;

export default function CanonicalProfile() {
  const { runId, run, schema, version } = useApp();
  const [page, setPage] = useState(0);
  const [idx, setIdx] = useState(0);
  const [showJson, setShowJson] = useState(false);
  const [lin, setLin] = useState<string | null>(null);
  const data = useAsync(() => (runId ? api.canonical(runId, PAGE, page * PAGE) : Promise.resolve(null)), [runId, page, version]);

  if (!run) return <Empty>No mapping run yet.</Empty>;
  if (data.error) return <ErrorBox>{data.error}</ErrorBox>;
  if (run.status !== "completed") return <Empty>{run.label} is {run.status}.</Empty>;
  const recs = data.data?.records ?? [];
  const total = data.data?.total ?? 0;
  const rec = recs[Math.min(idx, recs.length - 1)];
  if (!rec || !schema) return null;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {recs.map((r, i) => (
          <button key={r.id} onClick={() => { setIdx(i); setLin(null); }} className={`rounded-lg border px-3 py-1.5 text-sm ${i === idx ? "border-indigo-600 bg-indigo-600 text-white" : "border-slate-300 bg-white text-slate-600"}`}>
            {String(r.flat.name ?? `Record ${r.record_index + 1}`)}
          </button>
        ))}
        {pages > 1 && (
          <span className="ml-2 flex items-center gap-2 text-xs text-slate-500">
            <Button variant="ghost" disabled={page === 0} onClick={() => { setPage(page - 1); setIdx(0); }}>Prev</Button>
            records {page * PAGE + 1}-{Math.min(total, (page + 1) * PAGE)} of {total.toLocaleString()}
            <Button variant="ghost" disabled={page >= pages - 1} onClick={() => { setPage(page + 1); setIdx(0); }}>Next</Button>
          </span>
        )}
      </div>
      <div className="grid gap-5 lg:grid-cols-3">
        <Card title="Canonical profile" className="lg:col-span-2" right={<Button variant="ghost" onClick={() => setShowJson((v) => !v)}>{showJson ? "Readable view" : "JSON view"}</Button>}>
          {showJson ? <Json data={{ profile: rec.profile }} max="max-h-[32rem]" /> : (
            <dl className="divide-y divide-slate-100 text-sm">
              {schema.fields.map((f) => {
                const key = f.field;
                const v = rec.flat[key];
                const l = rec.lineage[key];
                return (
                  <div key={f.field} className="py-2">
                    <div className="grid grid-cols-[150px_1fr_auto] items-start gap-3">
                      <dt className="text-slate-500">{f.label}</dt>
                      <dd className={v === undefined ? "italic text-slate-300" : "text-slate-900"}>{v === undefined ? "not populated" : typeof v === "object" ? <pre className="whitespace-pre-wrap font-mono text-xs">{JSON.stringify(v, null, 1)}</pre> : String(v)}</dd>
                      <div className="flex items-center gap-2"><OwnerBadge owner={f.owner} />
                        {l && <button className="text-xs font-medium text-indigo-600" onClick={() => setLin(lin === key ? null : key)}>lineage</button>}</div>
                    </div>
                    {l && lin === key && (
                      <div className="mt-2 rounded-lg bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-600">
                        <div>{key}</div><div>↓ source field: <b>{l.source_field}</b></div><div>↓ source value: {l.source_value}</div>
                        <div>↓ LLM: {l.model} · confidence {l.confidence.toFixed(2)}{l.review_state === "approved" ? " · human-approved" : ""}</div>
                        <div>↓ schema {l.schema_version} · prompt {l.prompt_version}</div><div>↓ mapping run: {l.mapping_run}{l.mapping_id ? ` · mapping #${l.mapping_id}` : ""}</div>
                      </div>
                    )}
                  </div>
                );
              })}
            </dl>
          )}
        </Card>
        <Card title="Raw source record (preserved, never overwritten)"><Json data={rec.raw} max="max-h-[32rem]" /></Card>
      </div>
    </>
  );
}
