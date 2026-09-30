import { Fragment, useState } from "react";
import { api } from "../services/api";
import MappingDetail from "../components/MappingDetail";
import { useApp, useAsync } from "../components/AppContext";
import { Badge, Button, Card, Empty, ErrorBox, OwnerBadge, Table, trunc } from "../components/ui";
import type { Mapping } from "../types";

function ReviewCard({ m, fields, onDone }: { m: Mapping; fields: string[]; onDone: () => void }) {
  const [sel, setSel] = useState(m.target_field ?? "");
  const [err, setErr] = useState<string | null>(null);
  const act = async (a: "approve" | "reject" | "skip") => {
    setErr(null);
    try { await api.review(m.id, a, sel || undefined); onDone(); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50/50 p-4 text-sm">
      <div className="grid gap-x-8 gap-y-1 sm:grid-cols-2">
        <div>Source: <code className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">{m.source_field}</code> <span className="text-xs text-slate-400">record {m.record_index + 1}</span></div>
        <div>Value: <b>{trunc(m.source_value, 70)}</b></div>
        <div>LLM suggestion: <Badge kind={m.status}>{m.status}</Badge> <span className="text-xs text-slate-500">conf {m.confidence.toFixed(2)}</span></div>
        <div className="text-slate-600 sm:col-span-2">{m.reason}</div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select value={sel} onChange={(e) => setSel(e.target.value)} className="rounded border border-slate-300 bg-white px-2 py-1.5">
          <option value="">Select Canonical Field…</option>
          {fields.map((f) => <option key={f}>{f}</option>)}
        </select>
        <Button onClick={() => act("approve")} disabled={!sel}>Approve</Button>
        <Button variant="danger" onClick={() => act("reject")}>Reject</Button>
        <Button variant="ghost" onClick={() => act("skip")}>Skip</Button>
      </div>
      {err && <div className="mt-2"><ErrorBox>{err}</ErrorBox></div>}
    </div>
  );
}

export default function AIMapping() {
  const { runId, run, schema, version, bump } = useApp();
  const [rec, setRec] = useState<number | "all">("all");
  const [open, setOpen] = useState<number | null>(null);
  const [examplesVersion, setEV] = useState(0);
  const data = useAsync(() => (runId ? api.mappings(runId) : Promise.resolve(null)), [runId, version]);
  const examples = useAsync(() => api.examples(), [examplesVersion, version]);
  const saved = useAsync(() => api.sourceMappings(), [examplesVersion, version]);
  const fieldDec = useAsync(() => api.fieldDecisions(), [examplesVersion, version]);

  if (!run) return <Empty>No mapping run yet. Go to Source Data or Dashboard to run one.</Empty>;
  if (data.error) return <ErrorBox>{data.error}</ErrorBox>;
  if (run.status !== "completed") return <Empty>{run.label} is {run.status}. {run.error_message}</Empty>;
  const all = data.data?.mappings ?? [];
  const shown = rec === "all" ? all : all.filter((m) => m.record_index === rec);
  const nRecords = Math.max(0, ...all.map((m) => m.record_index + 1));
  const review = all.filter((m) => (m.status === "ambiguous" || m.status === "unmapped") && m.review_state !== "skipped" && m.review_state !== "rejected");
  const fields = schema?.fields.map((f) => f.field) ?? [];
  const done = () => { bump(); setEV((v) => v + 1); };

  return (
    <>
      <Card title="LLM mapping flow" right={
        <select value={rec} onChange={(e) => setRec(e.target.value === "all" ? "all" : Number(e.target.value))} className="rounded border border-slate-300 bg-white px-2 py-1 text-xs">
          <option value="all">All records</option>
          {Array.from({ length: nRecords }, (_, i) => <option key={i} value={i}>Record {i + 1}</option>)}
        </select>}>
        <div className="max-w-3xl">
        <div className="mb-2 grid grid-cols-[1fr_90px_1fr_60px] gap-2 border-b border-slate-200 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          <span>Source field</span><span /><span>Canonical field</span><span className="text-right">Conf.</span>
        </div>
        <div className="space-y-1.5">
          {shown.map((m) => (
            <div key={m.id} className="grid grid-cols-[1fr_90px_1fr_60px] items-center gap-2 text-sm">
              <div className="min-w-0"><code className="text-slate-800">{m.source_field}</code><div className="truncate text-[11px] text-slate-400">{trunc(m.source_value, 38)}</div></div>
              <div className="flex items-center gap-1 text-xs text-indigo-500"><span className="h-px flex-1 bg-indigo-200" /><span className="rounded bg-indigo-50 px-1.5 py-0.5 font-medium">🧠 LLM</span><span>▸</span></div>
              <div className="min-w-0">
                {m.status === "mapped" ? <code className="font-medium text-indigo-700">{m.target_field}</code>
                  : <span className="flex items-center gap-1.5"><Badge kind={m.status}>{m.status}</Badge>{m.target_field && <code className="text-xs text-slate-400">? {m.target_field}</code>}</span>}
              </div>
              <div className="text-right tabular-nums text-slate-600">{Math.round(m.confidence * 100)}%</div>
            </div>
          ))}
        </div>
        </div>
        <p className="mt-3 text-[11px] text-slate-400">Confidence is <b>LLM-reported</b>. It is not a statistically calibrated probability of correctness. Bands: High ≥ {run.thresholds.high_threshold}, Review ≥ {run.thresholds.review_threshold}, else Ambiguous.</p>
      </Card>

      {review.length > 0 && (
        <Card title={`⚠ ${review.length} fields need review`}>
          <div className="space-y-3">{review.map((m) => <ReviewCard key={m.id} m={m} fields={fields} onDone={done} />)}</div>
        </Card>
      )}

      <Card title="Mapping table (click a row to explain)">
        <Table head={["Rec", "Source field", "Source value", "Canonical field", "Owner", "ETL can populate", "LLM conf.", "Status"]}>
          {shown.map((m) => (
            <Fragment key={m.id}>
              <tr className="cursor-pointer hover:bg-slate-50" onClick={() => setOpen(open === m.id ? null : m.id)}>
                <td className="px-3 py-2 text-slate-400">{m.record_index + 1}</td>
                <td className="px-3 py-2 font-mono text-xs">{m.source_field}</td>
                <td className="px-3 py-2 text-slate-600">{trunc(m.source_value, 34)}</td>
                <td className="px-3 py-2 font-mono text-xs text-indigo-700">{m.target_field ?? "—"}</td>
                <td className="px-3 py-2"><OwnerBadge owner={m.owner} /></td>
                <td className="px-3 py-2">{m.status === "mapped" ? (m.etl_can_populate ? "Yes" : "No") : "No"}</td>
                <td className="px-3 py-2 tabular-nums">{Math.round(m.confidence * 100)}% <Badge kind={m.band ?? "unmapped"}>{m.band}</Badge></td>
                <td className="px-3 py-2"><Badge kind={m.status}>{m.status}</Badge></td>
              </tr>
              {open === m.id && <tr><td colSpan={8} className="p-0"><MappingDetail m={m} run={run} /></td></tr>}
            </Fragment>
          ))}
        </Table>
      </Card>

      <Card title="Saved field decisions (Claude decides once per distinct source field, then the decision is reused)">
        {(fieldDec.data ?? []).length === 0 ? <p className="text-sm text-slate-400">None yet. They are saved when a run uses “map each distinct field once”.</p> : (() => {
          const bySource = new Map<string, { n: number; reused: number; model: string }>();
          fieldDec.data!.forEach((d) => { const c = bySource.get(d.source_name) ?? { n: 0, reused: 0, model: d.model }; c.n += 1; c.reused += d.times_reused; bySource.set(d.source_name, c); });
          return (
            <Table head={["Source", "Saved field decisions", "Times reused", "Decided by", ""]}>
              {[...bySource.entries()].map(([name, c]) => (
                <tr key={name}>
                  <td className="px-3 py-2 font-medium">{name}</td><td className="px-3 py-2 tabular-nums">{c.n}</td>
                  <td className="px-3 py-2 tabular-nums">{c.reused}</td><td className="px-3 py-2 text-xs text-slate-500">{c.model}</td>
                  <td className="px-3 py-2 text-right"><button className="text-xs text-rose-600" onClick={async () => { await api.forgetFieldDecisions(name); setEV((v) => v + 1); }}>forget</button></td>
                </tr>
              ))}
            </Table>
          );
        })()}
      </Card>

      <Card title="Saved source mappings (one sample record per source, then reused)">
        {(saved.data ?? []).length === 0 ? <p className="text-sm text-slate-400">None yet. They are saved when a run maps a source in “map once per source” mode.</p> : (
          <Table head={["Source (field-name signature)", "Fields", "Decisions", "Decided by", "Reused", ""]}>
            {saved.data!.map((s) => (
              <tr key={s.id} className="align-top">
                <td className="px-3 py-2 font-mono text-xs text-slate-500">{s.signature}</td>
                <td className="px-3 py-2 text-xs text-slate-600">{trunc(s.fields.join(", "), 60)}</td>
                <td className="px-3 py-2 tabular-nums">{s.decisions.length}{s.decisions.some((d) => d.human_approved) && <span className="ml-1 text-xs text-indigo-600">· human-approved</span>}</td>
                <td className="px-3 py-2 text-xs text-slate-500">{s.model} · run {s.created_run_id}</td>
                <td className="px-3 py-2 tabular-nums">{s.times_reused}×</td>
                <td className="px-3 py-2 text-right"><button className="text-xs text-rose-600" onClick={async () => { await api.deleteSourceMapping(s.id); setEV((v) => v + 1); }}>forget</button></td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card title="Approved mapping examples (context for Claude, not rules)" right={
        <Button variant="ghost" onClick={async () => { await api.seedExamples(); setEV((v) => v + 1); }}>Load sample examples</Button>}>
        <p className="mb-3 text-xs text-slate-500">Human-approved mappings are passed to Claude as examples on the next run. The LLM still makes every final decision; nothing is matched deterministically.</p>
        {(examples.data ?? []).length === 0 ? <p className="text-sm text-slate-400">None yet. Approve a mapping above to add one.</p> : (
          <ul className="divide-y divide-slate-100 text-sm">
            {examples.data!.map((e) => (
              <li key={e.id} className="flex items-center justify-between py-1.5">
                <span><code>{e.source_field}</code> <span className="text-slate-300">→</span> <code className="text-indigo-700">{e.target_field}</code> <span className="text-xs text-slate-400">{trunc(e.example_value, 30)}</span></span>
                <button className="text-xs text-rose-600" onClick={async () => { await api.deleteExample(e.id); setEV((v) => v + 1); }}>remove</button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
