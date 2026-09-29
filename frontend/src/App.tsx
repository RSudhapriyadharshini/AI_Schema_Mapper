import { useEffect, useState } from "react";
import { AppProvider, useApp } from "./components/AppContext";
import Dashboard from "./pages/Dashboard";
import SourceData from "./pages/SourceData";
import AIMapping from "./pages/AIMapping";
import CanonicalProfile from "./pages/CanonicalProfile";
import CoverageAnalysis from "./pages/CoverageAnalysis";
import DraftDatabase from "./pages/DraftDatabase";
import MappingLogs from "./pages/MappingLogs";
import CanonicalSchemaPage from "./pages/CanonicalSchemaPage";

const TABS = [
  ["dashboard", "Dashboard", Dashboard],
  ["source", "Source Data", SourceData],
  ["mapping", "AI Mapping", AIMapping],
  ["profile", "Canonical Profile", CanonicalProfile],
  ["coverage", "Coverage Analysis", CoverageAnalysis],
  ["draft", "Draft Database", DraftDatabase],
  ["logs", "Mapping Logs", MappingLogs],
  ["schema", "Canonical Schema", CanonicalSchemaPage],
] as const;

function Shell() {
  const [tab, setTab] = useState(() => window.location.hash.slice(1) || "dashboard");
  const { runs, runId, setRunId, health } = useApp();
  useEffect(() => {
    const f = () => setTab(window.location.hash.slice(1) || "dashboard");
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  const Page = (TABS.find((t) => t[0] === tab) ?? TABS[0])[2];

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-6 py-3">
          <div className="text-sm font-semibold tracking-tight text-slate-900">Experience.com <span className="font-normal text-slate-500">· schema mapping prototype</span></div>
          <div className="flex items-center gap-3 text-xs text-slate-500">
            {health && !health.api_key_configured && <span className="rounded bg-rose-50 px-2 py-1 font-medium text-rose-700">ANTHROPIC_API_KEY not set</span>}
            {health && <span>Model: <b className="text-slate-700">{health.model}</b></span>}
            {runs.length > 0 && (
              <label className="flex items-center gap-1.5">Run
                <select value={runId ?? ""} onChange={(e) => setRunId(Number(e.target.value))} className="rounded border border-slate-300 bg-white px-1.5 py-1 text-slate-700">
                  {runs.map((r) => <option key={r.id} value={r.id}>{r.label} · {r.status}</option>)}
                </select>
              </label>
            )}
          </div>
        </div>
        <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4">
          {TABS.map(([key, label]) => (
            <a key={key} href={`#${key}`} className={`whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium ${tab === key ? "border-indigo-600 text-indigo-700" : "border-transparent text-slate-500 hover:text-slate-800"}`}>{label}</a>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-7xl space-y-5 px-6 py-6"><Page /></main>
    </div>
  );
}

export default function App() {
  return <AppProvider><Shell /></AppProvider>;
}
