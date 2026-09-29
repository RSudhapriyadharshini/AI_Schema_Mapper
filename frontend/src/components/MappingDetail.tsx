import type { Mapping, Run } from "../types";
import { Badge, OwnerBadge } from "./ui";

export default function MappingDetail({ m, run }: { m: Mapping; run: Run }) {
  const label = (t: string) => <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{t}</div>;
  return (
    <div className="grid gap-6 bg-slate-50 p-4 text-sm md:grid-cols-3">
      <div className="space-y-2">
        {label("Source")}
        <div>Field: <code className="rounded bg-white px-1.5 py-0.5 ring-1 ring-slate-200">{m.source_field}</code></div>
        <div className="break-words">Value: <span className="text-slate-700">{m.source_value}</span></div>
        <div className="text-xs text-slate-400">Record {m.record_index + 1}</div>
      </div>
      <div className="space-y-2">
        {label("LLM decision")}
        <div>Canonical field: <b>{m.target_field ?? "—"}</b></div>
        <div>Normalized value: <span className="break-words text-slate-700">{m.target_value ?? "—"}</span></div>
        <div>LLM-reported confidence: <b>{m.confidence.toFixed(2)}</b> <Badge kind={m.band ?? "unmapped"}>{m.band ?? "—"}</Badge></div>
        <div className="flex items-center gap-2">Owner: <OwnerBadge owner={m.owner} /> · ETL can populate: <b>{m.etl_can_populate ? "Yes" : "No"}</b></div>
        <div>LLM said: <Badge kind={m.llm_status}>{m.llm_status}</Badge> {m.review_state && <span className="text-xs text-slate-500">· review: {m.review_state}</span>}</div>
      </div>
      <div className="space-y-2">
        {label("Reason (from the LLM)")}
        <p className="text-slate-700">{m.reason}</p>
        {m.validation_note && <p className="text-xs text-amber-700">Validation: {m.validation_note}</p>}
        <div className="pt-1 text-xs text-slate-500">
          Schema {run.schema_version} · Model {run.model} · Prompt {run.prompt_version} · {run.label}
        </div>
      </div>
    </div>
  );
}
