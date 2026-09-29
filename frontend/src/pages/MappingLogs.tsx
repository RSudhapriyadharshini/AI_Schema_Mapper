import { Fragment, useState } from "react";
import { api } from "../services/api";
import { useApp, useAsync } from "../components/AppContext";
import { Badge, Card, Empty, ErrorBox, Table } from "../components/ui";

export default function MappingLogs() {
  const { runId, run, version } = useApp();
  const [open, setOpen] = useState<number | null>(null);
  const d = useAsync(() => (runId ? api.logs(runId) : Promise.resolve(null)), [runId, version]);
  if (!run) return <Empty>No mapping run yet.</Empty>;
  if (d.error) return <ErrorBox>{d.error}</ErrorBox>;
  const calls = d.data?.calls ?? [];

  return (
    <>
      <Card title={`Run ${run.label}: versions`}>
        <dl className="grid gap-x-8 gap-y-1 text-sm sm:grid-cols-3">
          {([["Mapping run ID", run.label], ["Status", run.status], ["Mapping model", run.model], ["Mapping prompt version", run.prompt_version],
            ["Canonical schema version", run.schema_version], ["Crawler version", run.crawler_version], ["Extraction prompt version", run.extraction_prompt_version],
            ["Source file", run.source_file], ["Created", run.created_at]] as const).map(([k, v]) => (
            <div key={k}><dt className="text-xs text-slate-400">{k}</dt><dd className="font-medium text-slate-800">{v}</dd></div>
          ))}
        </dl>
        {run.error_message && <div className="mt-3"><ErrorBox><b>{run.error_type}</b>: {run.error_message}</ErrorBox></div>}
      </Card>
      <Card title="Claude API calls (one per source record)">
        {calls.length === 0 ? <p className="text-sm text-slate-400">No calls logged for this run.</p> : (
          <Table head={["Record", "Attempt", "Status", "Stop reason", "Tokens in/out", "Latency", ""]}>
            {calls.map((c) => (
              <Fragment key={c.id}>
                <tr>
                  <td className="px-3 py-2">{c.record_index + 1}</td><td className="px-3 py-2">{c.attempt}</td>
                  <td className="px-3 py-2"><Badge kind={c.status === "ok" ? "mapped" : c.status === "invalid" ? "ambiguous" : "missing"}>{c.status}</Badge>{c.error && <span className="ml-2 text-xs text-rose-600">{c.error.slice(0, 80)}</span>}</td>
                  <td className="px-3 py-2 text-slate-500">{c.stop_reason ?? "—"}</td>
                  <td className="px-3 py-2 tabular-nums">{c.input_tokens ?? "—"} / {c.output_tokens ?? "—"}</td>
                  <td className="px-3 py-2 tabular-nums">{c.latency_ms ? `${(c.latency_ms / 1000).toFixed(1)}s` : "—"}</td>
                  <td className="px-3 py-2 text-right"><button className="text-xs font-medium text-indigo-600" onClick={() => setOpen(open === c.id ? null : c.id)}>{open === c.id ? "Hide" : "Request / response"}</button></td>
                </tr>
                {open === c.id && (
                  <tr><td colSpan={7} className="px-3 pb-3"><div className="grid gap-3 lg:grid-cols-2">
                    <div><div className="mb-1 text-xs font-semibold text-slate-400">REQUEST (messages)</div><pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-3 text-[11px] text-slate-100">{c.request_text}</pre></div>
                    <div><div className="mb-1 text-xs font-semibold text-slate-400">RESPONSE (raw from Claude)</div><pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-900 p-3 text-[11px] text-slate-100">{c.response_text ?? "—"}</pre></div>
                  </div></td></tr>
                )}
              </Fragment>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
