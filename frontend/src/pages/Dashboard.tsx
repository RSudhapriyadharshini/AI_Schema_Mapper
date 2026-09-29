import { api } from "../services/api";
import RunPanel from "../components/RunPanel";
import { useApp, useAsync } from "../components/AppContext";
import { Badge, Bar, Card, Empty, ErrorBox, OWNER_BAR, OwnerBadge, Stat, pct } from "../components/ui";

const FLOW = ["Source websites / APIs", "AI crawler / ETL", "Raw extracted data", "LLM schema mapper", "Canonical profile", "Validation", "Draft database", "Dedup / enrichment (future)"];

export default function Dashboard() {
  const { runId, run, version } = useApp();
  const cov = useAsync(() => (runId && run?.status === "completed" ? api.coverage(runId) : Promise.resolve(null)), [runId, run?.status, version]);
  const maps = useAsync(() => (runId && run?.status === "completed" ? api.mappings(runId) : Promise.resolve(null)), [runId, run?.status, version]);
  const m = cov.data?.metrics;

  const flows = new Map<string, { source: string; target: string; owner: string | null; n: number; conf: number }>();
  maps.data?.mappings.filter((x) => x.status === "mapped").forEach((x) => {
    const k = `${x.source_field}→${x.target_field}`;
    const cur = flows.get(k) ?? { source: x.source_field, target: x.target_field!, owner: x.owner, n: 0, conf: 0 };
    cur.n += 1; cur.conf += x.confidence; flows.set(k, cur);
  });
  const flowList = [...flows.values()].sort((a, b) => b.n - a.n).slice(0, 14);

  return (
    <>
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">AI DATA NORMALIZATION</h1>
        <p className="mt-1 text-sm text-slate-500">Crawler → LLM Mapping → Canonical Profile → Draft Database</p>
      </div>

      <Card title="Data flow">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {FLOW.map((s, i) => (
            <span key={s} className="flex items-center gap-2">
              <span className={`rounded-md border px-2.5 py-1.5 font-medium ${s.startsWith("LLM") ? "border-indigo-300 bg-indigo-50 text-indigo-700" : s.includes("future") ? "border-dashed border-slate-300 text-slate-400" : "border-slate-200 bg-white text-slate-600"}`}>{s.startsWith("LLM") ? "🧠 " : ""}{s}</span>
              {i < FLOW.length - 1 && <span className="text-slate-300">→</span>}
            </span>
          ))}
        </div>
      </Card>

      {m ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Total profiles" value={m.profiles} sub={run?.source_file} />
          <Stat label="Canonical fields" value={m.canonical_fields} sub={`${m.populated_fields} populated · ${m.missing_fields} missing`} />
          <Stat label="Fields extracted" value={m.fields_extracted} sub="source values across all records" />
          <Stat label="Fields mapped" value={m.fields_mapped} tone="emerald" sub={`${m.canonical_mappings} canonical mappings`} />
          <Stat label="Unmapped fields" value={m.fields_unmapped} />
          <Stat label="Ambiguous fields" value={m.fields_ambiguous} tone={m.fields_ambiguous ? "amber" : "slate"} sub={m.fields_ambiguous ? "⚠ review on AI Mapping tab" : undefined} />
          <Stat label="ETL coverage" value={pct(m.etl_coverage)} tone="indigo" sub={`${m.etl_owned_populated} of ${m.etl_owned_total} ETL-owned fields`} />
          <Stat label="Profile completeness" value={pct(m.profile_completeness)} tone="indigo" sub="avg. across profiles" />
        </div>
      ) : cov.error ? <ErrorBox>{cov.error}</ErrorBox>
        : <Empty>No completed mapping run yet. Load the sample crawler data and click <b>Run AI Schema Mapping</b>.</Empty>}

      <RunPanel compact />

      {m && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card title="Field mapping (decided by the LLM)">
            <div className="divide-y divide-slate-100 text-sm">
              {flowList.map((f) => (
                <div key={f.source + f.target} className="flex items-center gap-2 py-1.5">
                  <code className="w-40 shrink-0 truncate text-slate-700">{f.source}</code>
                  <span className="text-slate-300">→</span>
                  <code className="min-w-0 flex-1 truncate text-indigo-700">{f.target}</code>
                  <span className="w-12 text-right tabular-nums text-slate-500">{pct(f.conf / f.n)}</span>
                  <span className="w-28 text-right"><OwnerBadge owner={f.owner as never} /></span>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-slate-400">Confidence is LLM-reported (mean across records), not a calibrated probability.</p>
          </Card>
          <div className="space-y-5">
            <Card title="Missing fields">
              {cov.data!.missing.length === 0 ? <p className="text-sm text-emerald-700">Every canonical field has a value.</p> : (
                <ul className="space-y-1.5 text-sm">
                  {cov.data!.missing.map((x) => (
                    <li key={x.field} className="flex items-center justify-between gap-2"><span>⚠ <code>{x.field}</code></span><OwnerBadge owner={x.owner} /></li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Profile field ownership">
              <div className="space-y-2">
                {cov.data!.ownership.map((o) => (
                  <div key={o.owner}>
                    <div className="mb-1 flex justify-between text-xs"><span className="font-medium">{o.owner}</span><span className="text-slate-500">{o.total} fields · {o.populated} populated</span></div>
                    <Bar value={o.total} max={cov.data!.metrics.canonical_fields} className={OWNER_BAR[o.owner]} />
                  </div>
                ))}
              </div>
            </Card>
            {m.fields_ambiguous > 0 && <Badge kind="ambiguous">⚠ {m.fields_ambiguous} ambiguous mappings</Badge>}
          </div>
        </div>
      )}
    </>
  );
}
