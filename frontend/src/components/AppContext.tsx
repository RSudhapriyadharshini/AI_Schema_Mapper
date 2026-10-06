import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "../services/api";
import type { CanonicalSchema, Health, MappingMode, Run, Step, Upload } from "../types";

interface Ctx {
  health: Health | null;
  schema: CanonicalSchema | null;
  runs: Run[];
  run: Run | null;
  runId: number | null;
  setRunId: (id: number) => void;
  upload: Upload | null;
  setUpload: (u: Upload | null) => void;
  activeSteps: Step[] | null;
  activeRun: Run | null;
  starting: boolean;
  startError: string | null;
  startRun: (useExamples: boolean, mode: MappingMode, strict: boolean) => Promise<void>;
  version: number;
  bump: () => void;
}

const C = createContext<Ctx>(null as unknown as Ctx);
export const useApp = () => useContext(C);

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; loading: boolean } {
  const [state, set] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  useEffect(() => {
    let alive = true;
    set((s) => ({ ...s, loading: true, error: null }));
    fn().then((data) => alive && set({ data, error: null, loading: false }))
      .catch((e: unknown) => alive && set({ data: null, error: e instanceof Error ? e.message : String(e), loading: false }));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [schema, setSchema] = useState<CanonicalSchema | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunIdState] = useState<number | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [activeSteps, setActiveSteps] = useState<Step[] | null>(null);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const timer = useRef<number | null>(null);

  const bump = useCallback(() => setVersion((v) => v + 1), []);
  const refreshRuns = useCallback(async () => {
    const r = await api.runs();
    setRuns(r);
    return r;
  }, []);
  const setRunId = (id: number) => { setRunIdState(id); try { localStorage.setItem("runId", String(id)); } catch { /* ignore */ } };

  // initial load
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.schema().then(setSchema).catch(() => undefined);
    (async () => {
      try {
        const r = await refreshRuns();
        let saved: number | null = null;
        try { saved = Number(localStorage.getItem("runId")) || null; } catch { /* ignore */ }
        const pick = r.find((x) => x.id === saved) ?? r.find((x) => x.status === "completed") ?? r[0];
        if (pick) setRunIdState(pick.id);
        const ups = await api.listUploads();
        if (ups[0]) setUpload(await api.getUpload(ups[0].id));
        const running = r.find((x) => x.status === "running");
        if (running) poll(running.id);
      } catch { /* backend offline: pages show their own errors */ }
    })();
    return () => { if (timer.current) window.clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll real backend job status until the run finishes.
  const poll = useCallback((id: number) => {
    const tick = async () => {
      try {
        const st = await api.runStatus(id);
        setActiveSteps(st.steps);
        setActiveRun(st.run);
        if (st.run.status === "running") { timer.current = window.setTimeout(tick, 600); return; }
        await refreshRuns();
        if (st.run.status === "completed") setRunId(id);
        bump();
      } catch {
        timer.current = window.setTimeout(tick, 1500);
      }
    };
    void tick();
  }, [refreshRuns, bump]);

  const startRun = async (useExamples: boolean, mode: MappingMode, strict: boolean) => {
    if (!upload) return;
    setStarting(true); setStartError(null);
    try {
      const { run_id } = await api.startRun(upload.id, useExamples, mode, strict);
      setActiveSteps(null); setActiveRun(null);
      poll(run_id);
    } catch (e) {
      setStartError(e instanceof ApiError ? e.message : String(e));
    } finally { setStarting(false); }
  };

  const run = runs.find((r) => r.id === runId) ?? null;
  return (
    <C.Provider value={{ health, schema, runs, run, runId, setRunId, upload, setUpload, activeSteps, activeRun, starting, startError, startRun, version, bump }}>
      {children}
    </C.Provider>
  );
}
