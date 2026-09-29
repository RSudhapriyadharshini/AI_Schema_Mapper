import { useState } from "react";
import { api } from "../services/api";
import { useApp, useAsync } from "../components/AppContext";
import { Card, Empty, ErrorBox, Table, trunc } from "../components/ui";

const TABLES = ["mapping_runs", "source_records", "field_mappings", "canonical_records"];

export default function DraftDatabase() {
  const { runId, version } = useApp();
  const [table, setTable] = useState(TABLES[3]);
  const [scope, setScope] = useState<"run" | "all">("run");
  const d = useAsync(() => api.table(table, scope === "run" ? runId ?? undefined : undefined), [table, scope, runId, version]);
  const cols = d.data?.rows[0] ? Object.keys(d.data.rows[0]) : [];

  return (
    <>
      <Card title="Draft database (SQLite)" right={
        <select value={scope} onChange={(e) => setScope(e.target.value as "run" | "all")} className="rounded border border-slate-300 bg-white px-2 py-1 text-xs">
          <option value="run">Selected run</option><option value="all">All runs</option>
        </select>}>
        <div className="mb-4 flex flex-wrap gap-2">
          {TABLES.map((t) => (
            <button key={t} onClick={() => setTable(t)} className={`rounded-lg border px-3 py-1.5 text-sm ${t === table ? "border-indigo-600 bg-indigo-600 text-white" : "border-slate-300 bg-white text-slate-600"}`}>
              {t} <span className="opacity-70">({d.data?.counts[t] ?? "…"})</span>
            </button>
          ))}
        </div>
        {d.error && <ErrorBox>{d.error}</ErrorBox>}
        {d.data && d.data.rows.length === 0 && <Empty>No rows in {table} for this selection.</Empty>}
        {d.data && d.data.rows.length > 0 && (
          <Table head={cols}>
            {d.data.rows.map((r, i) => (
              <tr key={i} className="align-top">
                {cols.map((c) => <td key={c} className="max-w-xs px-3 py-2 font-mono text-xs text-slate-600" title={String(r[c] ?? "")}>{trunc(r[c] === null ? "null" : String(r[c]), 60)}</td>)}
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="What happens next">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="rounded-md border border-indigo-300 bg-indigo-50 px-3 py-1.5 font-medium text-indigo-700">Schema mapping → Draft DB (this prototype)</span>
          <span className="text-slate-300">→</span>
          <span className="rounded-md border border-dashed border-slate-300 px-3 py-1.5 text-slate-400">Future: deduplication</span>
          <span className="text-slate-300">→</span>
          <span className="rounded-md border border-dashed border-slate-300 px-3 py-1.5 text-slate-400">Future: enrichment</span>
        </div>
        <p className="mt-2 text-xs text-slate-500">The mapper answers “what does this field mean?”. Deduplication answers “is this the same person?”. They are deliberately separate systems. Raw records are stored untouched in <code>source_records</code>; canonical records are derived from mappings.</p>
      </Card>
    </>
  );
}
