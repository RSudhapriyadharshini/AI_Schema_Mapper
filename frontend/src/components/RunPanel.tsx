import { useState } from "react";
import { api } from "../services/api";
import { useApp, useAsync } from "./AppContext";
import Pipeline from "./Pipeline";
import { Button, Card, ErrorBox } from "./ui";
import type { MappingMode } from "../types";

export default function RunPanel({ compact = false }: { compact?: boolean }) {
  const { upload, setUpload, starting, startError, startRun, activeSteps, activeRun, runId, run, version } = useApp();
  const [useExamples, setUseExamples] = useState(true);
  const [mode, setMode] = useState<MappingMode>("per_source");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const running = activeRun?.status === "running" || run?.status === "running";

  const stored = useAsync(() => (runId && !activeSteps ? api.runStatus(runId) : Promise.resolve(null)), [runId, activeSteps, version]);
  const steps = activeSteps ?? stored.data?.steps ?? null;
  const shownRun = activeSteps ? activeRun : stored.data?.run ?? null;

  const loadSample = async () => {
    setLoading(true); setErr(null);
    try { setUpload(await api.loadSample()); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setLoading(false); }
  };

  return (
    <Card title="Processing pipeline" right={shownRun && <span className="text-xs text-slate-500">{shownRun.label} · {shownRun.status}</span>}>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        {!upload && <Button onClick={loadSample} disabled={loading}>Load Sample Crawler Data</Button>}
        {upload && (
          <>
            <Button onClick={() => startRun(useExamples, mode)} disabled={starting || running}>{running ? "Mapping in progress…" : "Run AI Schema Mapping"}</Button>
            <span className="text-xs text-slate-500">on <b>{upload.file_name}</b> ({upload.num_records} records, {upload.num_fields} source fields)</span>
            <select value={mode} onChange={(e) => setMode(e.target.value as MappingMode)} className="rounded border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-700" title="How many Claude requests to make">
              <option value="per_source">Map once per source (saved, lowest cost)</option>
              <option value="per_source_relearn">Map once per source, ignore saved (re-learn)</option>
              <option value="per_record">Map every record (baseline, highest cost)</option>
            </select>
            {!compact && (
              <label className="flex items-center gap-1.5 text-xs text-slate-600">
                <input type="checkbox" checked={useExamples} onChange={(e) => setUseExamples(e.target.checked)} /> include approved mapping examples as context
              </label>
            )}
          </>
        )}
      </div>
      {(startError || err) && <div className="mb-3"><ErrorBox>{startError ?? err}</ErrorBox></div>}
      {shownRun?.status === "completed" && (
        <p className="mb-3 text-xs text-slate-500">
          <b>{shownRun.llm_requests}</b> Claude request(s) for <b>{shownRun.total_records}</b> records · tokens in/out {shownRun.input_tokens.toLocaleString()} / {shownRun.output_tokens.toLocaleString()} · mode: {shownRun.mapping_mode ?? "per_record"}
        </p>
      )}
      {steps ? <Pipeline steps={steps} run={shownRun} /> : <p className="text-sm text-slate-400">No run yet. Load crawler data, then run the mapping. Steps below reflect real backend job status.</p>}
    </Card>
  );
}
