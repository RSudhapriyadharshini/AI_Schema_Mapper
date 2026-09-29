import type { Run, Step } from "../types";

const ICON = { done: "✓", running: "●", failed: "✕", pending: "○" } as const;

export default function Pipeline({ steps, run }: { steps: Step[]; run?: Run | null }) {
  return (
    <div>
      <ol className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-9">
        {steps.map((s, i) => {
          const tone = {
            done: "border-emerald-200 bg-emerald-50 text-emerald-800",
            running: "border-indigo-300 bg-indigo-50 text-indigo-800",
            failed: "border-rose-300 bg-rose-50 text-rose-800",
            pending: "border-slate-200 bg-white text-slate-400",
          }[s.status];
          return (
            <li key={s.step_key} className={`rounded-lg border p-2.5 text-xs ${tone}`} title={s.detail ?? ""}>
              <div className="flex items-center gap-1.5 font-semibold">
                <span className={s.status === "running" ? "animate-pulse" : ""}>{ICON[s.status]}</span>
                <span>{i + 1}. {s.label}</span>
              </div>
              <div className="mt-1 min-h-8 break-words leading-snug opacity-80">{s.detail ?? ""}</div>
            </li>
          );
        })}
      </ol>
      {run?.status === "failed" && (
        <div role="alert" className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <b>{run.error_type}</b>: {run.error_message} <span className="text-rose-600">No mapping results were written for this run.</span>
        </div>
      )}
    </div>
  );
}
