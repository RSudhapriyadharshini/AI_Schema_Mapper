import { useEffect, useState } from "react";
import { api } from "../services/api";
import { useApp, useAsync } from "../components/AppContext";
import { Button, Card, ErrorBox, OwnerBadge, Table } from "../components/ui";

export default function CanonicalSchemaPage() {
  const { schema, bump } = useApp();
  const s = useAsync(() => api.settings(), []);
  const [high, setHigh] = useState(0.9);
  const [review, setReview] = useState(0.7);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => { if (s.data) { setHigh(s.data.high_threshold); setReview(s.data.review_threshold); } }, [s.data]);

  const save = async () => {
    try { await api.saveSettings({ high_threshold: high, review_threshold: review }); setMsg({ ok: true, text: "Saved. Applies to new runs." }); bump(); }
    catch (e) { setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) }); }
  };
  if (!schema) return <ErrorBox>Schema not loaded. Is the backend running?</ErrorBox>;

  return (
    <>
      <Card title={`Canonical schema · version ${schema.schema_version} · ${schema.fields.length} fields`}>
        <Table head={["Field", "Description", "Type", "Required", "Owner", "Source priority"]}>
          {schema.fields.map((f) => (
            <tr key={f.field} className="align-top">
              <td className="px-3 py-2 font-mono text-xs text-indigo-700">{f.field}</td>
              <td className="px-3 py-2 text-slate-600">{f.description}</td>
              <td className="px-3 py-2 text-xs">{f.data_type}</td>
              <td className="px-3 py-2">{f.required ? "Yes" : "No"}</td>
              <td className="px-3 py-2"><OwnerBadge owner={f.owner} /></td>
              <td className="px-3 py-2 text-xs text-slate-500">{f.source_priority.join(" › ")}</td>
            </tr>
          ))}
        </Table>
        <p className="mt-2 text-[11px] text-slate-400">Defined in <code>backend/app/schemas/canonical_schema.json</code>. The LLM may only choose targets from this list.</p>
      </Card>
      <Card title="LLM-reported confidence thresholds">
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <label className="text-xs text-slate-500">High ≥<input type="number" step="0.01" min="0" max="1" value={high} onChange={(e) => setHigh(Number(e.target.value))} className="mt-1 block w-24 rounded border border-slate-300 px-2 py-1.5 text-sm" /></label>
          <label className="text-xs text-slate-500">Review ≥ (below = ambiguous)<input type="number" step="0.01" min="0" max="1" value={review} onChange={(e) => setReview(Number(e.target.value))} className="mt-1 block w-24 rounded border border-slate-300 px-2 py-1.5 text-sm" /></label>
          <Button onClick={save}>Save</Button>
          {msg && <span className={`text-xs ${msg.ok ? "text-emerald-700" : "text-rose-700"}`}>{msg.text}</span>}
        </div>
        <p className="mt-2 text-xs text-slate-500">These are the model's own confidence self-reports, used to route mappings into High / Review / Ambiguous. They are not calibrated probabilities.</p>
      </Card>
    </>
  );
}
