import { api } from "../services/api";
import { useApp, useAsync } from "../components/AppContext";
import { Badge, Bar, Card, Empty, ErrorBox, OWNER_BAR, OwnerBadge, Stat, Table, pct, trunc } from "../components/ui";

export default function CoverageAnalysis() {
  const { runId, run, version } = useApp();
  const cov = useAsync(() => (runId && run?.status === "completed" ? api.coverage(runId) : Promise.resolve(null)), [runId, run?.status, version]);
  if (!run) return <Empty>No mapping run yet.</Empty>;
  if (cov.error) return <ErrorBox>{cov.error}</ErrorBox>;
  if (run.status !== "completed") return <Empty>{run.label} is {run.status}.</Empty>;
  const c = cov.data;
  if (!c) return null;
  const m = c.metrics;
  const icon = { populated: "✅", partial: "◐", missing: "⚠️" } as const;

  return (
    <>
      <div>
        <h1 className="text-xl font-bold text-slate-900">Coverage Analysis</h1>
        <p className="mt-1 text-sm text-slate-500">How much of the canonical profile can our ETL actually populate from this crawler data?</p>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <Stat label="Canonical field coverage" value={pct(m.field_coverage)} tone="indigo" sub={`${m.populated_fields} populated · ${m.missing_fields} missing of ${m.canonical_fields}`} />
        <Stat label="ETL coverage" value={pct(m.etl_coverage)} tone="indigo" sub={`${m.etl_owned_populated} of ${m.etl_owned_total} ETL-owned fields`} />
        <Stat label="Profile completeness" value={pct(m.profile_completeness)} tone="indigo" sub={`required fields only: ${pct(m.required_completeness)}`} />
      </div>
      <Card title="Formulas (calculated from the stored mappings)">
        <ul className="space-y-1.5 font-mono text-xs text-slate-600">
          <li><b>Field coverage</b> = canonical fields with a usable value in ≥1 profile ÷ all canonical fields = {m.populated_fields} ÷ {m.canonical_fields} = {pct(m.field_coverage)}</li>
          <li><b>ETL coverage</b> = ETL-owned fields populated from the crawler (ETL can populate = yes) ÷ ETL-owned fields = {m.etl_owned_populated} ÷ {m.etl_owned_total} = {pct(m.etl_coverage)}</li>
          <li><b>Profile completeness</b> = mean over {m.profiles} profiles of (fields with usable values ÷ {m.canonical_fields}) = {pct(m.profile_completeness)}</li>
        </ul>
      </Card>

      <Card title="Canonical field coverage">
        <Table head={["Canonical field", "Source field(s) found", "Sample value", "Fill", "Owner", "ETL can populate", "Status"]}>
          {c.fields.map((f) => (
            <tr key={f.field}>
              <td className="px-3 py-2"><div className="font-medium">{f.label}</div><code className="text-[11px] text-slate-400">{f.field}{f.required ? " · required" : ""}</code></td>
              <td className="px-3 py-2 font-mono text-xs">{f.source_fields.length ? f.source_fields.join(", ") : "—"}</td>
              <td className="px-3 py-2 text-slate-600">{trunc(f.sample_value, 32)}</td>
              <td className="px-3 py-2 tabular-nums text-slate-500">{f.records_populated}/{f.records_total}</td>
              <td className="px-3 py-2"><OwnerBadge owner={f.owner} /></td>
              <td className="px-3 py-2">{f.etl_can_populate ? "Yes" : "No"}</td>
              <td className="px-3 py-2 whitespace-nowrap">{icon[f.status]} <Badge kind={f.status}>{f.status}</Badge></td>
            </tr>
          ))}
        </Table>
        <p className="mt-2 text-[11px] text-slate-400">“ETL can populate” is computed from each field’s <code>source_priority</code> in the schema (is the crawler an accepted source?). A field can hold a crawler value yet be “No” when the crawler is not an accepted source.</p>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Profile field ownership">
          <div className="space-y-3">
            {c.ownership.map((o) => (
              <div key={o.owner}>
                <div className="mb-1 flex justify-between text-sm"><span className="font-medium">{o.owner}</span><span className="text-slate-500">{o.total} fields · {o.populated} populated · {o.missing} missing</span></div>
                <Bar value={o.total} max={m.canonical_fields} className={OWNER_BAR[o.owner]} />
              </div>
            ))}
          </div>
        </Card>
        <Card title="Fields still required">
          {c.missing.length === 0 ? <p className="text-sm text-emerald-700">No missing fields.</p> : (
            <div className="max-h-96 space-y-3 overflow-y-auto pr-1">
              {c.missing.map((x) => (
                <div key={x.field} className="rounded-lg border border-slate-200 p-3 text-sm">
                  <div className="flex items-center justify-between"><b>⚠ {x.field}</b><OwnerBadge owner={x.owner} /></div>
                  <div className="mt-1 text-slate-600"><span className="text-slate-400">Reason:</span> {x.reason}</div>
                  <div className="text-slate-600"><span className="text-slate-400">Recommended action:</span> {x.recommended_action}</div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Card title="Reverse mapping: many source names, one canonical field">
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {c.reverse_mapping.filter((r) => r.sources.length > 0).map((r) => (
            <div key={r.field} className="rounded-lg border border-slate-200 p-3 text-sm">
              <code className="font-semibold text-indigo-700">{r.field}</code>
              <ul className="mt-1.5 border-l-2 border-indigo-200 pl-3 text-slate-700">
                {r.sources.map((s) => <li key={s.source_field}><code>{s.source_field}</code>{s.count > 1 && <span className="text-xs text-slate-400"> ×{s.count}</span>}</li>)}
              </ul>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}
