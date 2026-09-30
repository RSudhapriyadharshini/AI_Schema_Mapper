import { Fragment, useRef, useState } from "react";
import { api } from "../services/api";
import RunPanel from "../components/RunPanel";
import { useApp } from "../components/AppContext";
import { Button, Card, ErrorBox, Json, Stat, Table, trunc } from "../components/ui";

export default function SourceData() {
  const { upload, setUpload } = useApp();
  const [crawler, setCrawler] = useState("");
  const [extraction, setExtraction] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const doUpload = async (f: File | undefined) => {
    if (!f) return;
    setBusy(true); setErr(null);
    try { setUpload(await api.uploadFile(f, crawler, extraction, sourceName)); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  };
  const sample = async () => {
    setBusy(true); setErr(null);
    try { setUpload(await api.loadSample()); } catch (e) { setErr(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  return (
    <>
      <Card title="Upload crawler data">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <input ref={fileRef} type="file" accept=".json,.csv" onChange={(e) => doUpload(e.target.files?.[0])} disabled={busy}
              className="block text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:text-sm file:font-medium hover:file:bg-slate-200" />
            <p className="mt-1 text-xs text-slate-400">JSON (array of records) or CSV (header row)</p>
          </div>
          <label className="text-xs text-slate-500">Source name<input value={sourceName} onChange={(e) => setSourceName(e.target.value)} placeholder="defaults to file name" className="mt-1 block rounded border border-slate-300 px-2 py-1.5 text-sm" /></label>
          <label className="text-xs text-slate-500">Crawler version<input value={crawler} onChange={(e) => setCrawler(e.target.value)} placeholder="e.g. crawler-2.3" className="mt-1 block rounded border border-slate-300 px-2 py-1.5 text-sm" /></label>
          <label className="text-xs text-slate-500">Extraction prompt version<input value={extraction} onChange={(e) => setExtraction(e.target.value)} placeholder="e.g. extract-1.4" className="mt-1 block rounded border border-slate-300 px-2 py-1.5 text-sm" /></label>
          <Button variant="ghost" onClick={sample} disabled={busy}>Load Sample Crawler Data</Button>
        </div>
        {err && <div className="mt-3"><ErrorBox>{err}</ErrorBox></div>}
      </Card>

      {upload && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="File" value={<span className="text-lg">{upload.file_name}</span>} />
            <Stat label="Format" value={upload.format} />
            <Stat label="Records" value={upload.num_records} />
            <Stat label="Unique source fields" value={upload.num_fields} sub={`nested fields as dotted paths · ${upload.fields_extracted} values · ${upload.dropped_empty} empty/placeholder values ignored`} />
          </div>
          <p className="text-xs text-slate-500">Source name: <b>{upload.source_name}</b> · Crawler version: <b>{upload.crawler_version}</b> · Extraction prompt version: <b>{upload.extraction_prompt_version}</b></p>
          <RunPanel />

          <Card title={`Source fields as extracted: ${upload.fields.length} distinct paths (note the inconsistent naming)`}>
            <div className="flex flex-wrap gap-1.5">
              {upload.fields.slice(0, 250).map((f) => <span key={f.field} className="rounded-md bg-slate-100 px-2 py-1 font-mono text-xs text-slate-700">{f.field}{f.records > 1 && <span className="ml-1 text-slate-400">×{f.records}</span>}</span>)}
            </div>
          </Card>

          <Card title="Raw records">
            <Table head={["#", "Fields", "Preview", ""]}>
              {upload.records.map((r, i) => (
                <Fragment key={i}>
                  <tr className="align-top">
                    <td className="px-3 py-2 text-slate-400">{i + 1}</td>
                    <td className="px-3 py-2 tabular-nums">{Object.keys(r).length}</td>
                    <td className="px-3 py-2 text-slate-600">{trunc(Object.entries(r).slice(0, 3).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "), 110)}</td>
                    <td className="px-3 py-2 text-right"><button className="text-xs font-medium text-indigo-600" onClick={() => setOpen(open === i ? null : i)}>{open === i ? "Hide" : "View JSON"}</button></td>
                  </tr>
                  {open === i && <tr><td colSpan={4} className="px-3 pb-3"><Json data={r} max="max-h-72" /></td></tr>}
                </Fragment>
              ))}
            </Table>
          </Card>
        </>
      )}
    </>
  );
}
