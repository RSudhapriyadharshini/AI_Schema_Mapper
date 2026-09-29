import { useState, type ReactNode } from "react";
import type { Owner } from "../types";

export const pct = (x: number | undefined) => (x === undefined ? "—" : `${(x * 100).toFixed(1).replace(/\.0$/, "")}%`);

export function Card({ title, children, right, className = "" }: { title?: string; children: ReactNode; right?: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-slate-200 bg-white p-5 shadow-sm ${className}`}>
      {(title || right) && (
        <div className="mb-3 flex items-center justify-between">
          {title && <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">{title}</h2>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, tone = "slate" }: { label: string; value: ReactNode; sub?: string; tone?: "slate" | "amber" | "emerald" | "indigo" }) {
  const c = { slate: "text-slate-900", amber: "text-amber-600", emerald: "text-emerald-600", indigo: "text-indigo-600" }[tone];
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-3xl font-semibold tabular-nums ${c}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-400">{sub}</div>}
    </div>
  );
}

const OWNER_STYLE: Record<string, string> = {
  ETL: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  PROFESSIONAL: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  OTHER_SOURCE: "bg-amber-50 text-amber-700 ring-amber-200",
  SYSTEM: "bg-slate-100 text-slate-600 ring-slate-300",
};
export const OWNER_BAR: Record<string, string> = { ETL: "bg-indigo-500", PROFESSIONAL: "bg-emerald-500", OTHER_SOURCE: "bg-amber-500", SYSTEM: "bg-slate-400" };

export function OwnerBadge({ owner }: { owner: Owner | null }) {
  if (!owner) return <span className="text-slate-300">—</span>;
  return <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${OWNER_STYLE[owner]}`}>{owner}</span>;
}

const STATUS_STYLE: Record<string, string> = {
  mapped: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  populated: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  partial: "bg-amber-50 text-amber-700 ring-amber-200",
  ambiguous: "bg-amber-50 text-amber-700 ring-amber-200",
  missing: "bg-rose-50 text-rose-700 ring-rose-200",
  unmapped: "bg-slate-100 text-slate-600 ring-slate-300",
  High: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  Review: "bg-amber-50 text-amber-700 ring-amber-200",
  Ambiguous: "bg-rose-50 text-rose-700 ring-rose-200",
  Approved: "bg-indigo-50 text-indigo-700 ring-indigo-200",
};
export function Badge({ children, kind }: { children: ReactNode; kind: string }) {
  return <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium capitalize ring-1 ring-inset ${STATUS_STYLE[kind] ?? STATUS_STYLE.unmapped}`}>{children}</span>;
}

export function Bar({ value, max, className = "bg-indigo-500" }: { value: number; max: number; className?: string }) {
  return (
    <div className="h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
      <div className={`h-full rounded-full transition-all ${className}`} style={{ width: `${max ? (value / max) * 100 : 0}%` }} />
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">{children}</div>;
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">{children}</div>;
}

export function Button({ children, onClick, disabled, variant = "primary", type = "button" }: {
  children: ReactNode; onClick?: () => void; disabled?: boolean; variant?: "primary" | "ghost" | "danger"; type?: "button" | "submit";
}) {
  const v = {
    primary: "bg-indigo-600 text-white hover:bg-indigo-700 disabled:bg-slate-300",
    ghost: "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:text-slate-300",
    danger: "border border-rose-200 bg-white text-rose-700 hover:bg-rose-50 disabled:text-slate-300",
  }[variant];
  return <button type={type} onClick={onClick} disabled={disabled} className={`rounded-lg px-3.5 py-2 text-sm font-medium transition ${v}`}>{children}</button>;
}

export function Json({ data, max = "max-h-96" }: { data: unknown; max?: string }) {
  return <pre className={`${max} overflow-auto rounded-lg bg-slate-900 p-4 text-xs leading-relaxed text-slate-100`}>{JSON.stringify(data, null, 2)}</pre>;
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead><tr className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
          {head.map((h) => <th key={h} className="whitespace-nowrap px-3 py-2 font-medium">{h}</th>)}
        </tr></thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
    </div>
  );
}

export function useToggle(initial = false): [boolean, () => void] {
  const [v, set] = useState(initial);
  return [v, () => set((x) => !x)];
}

export const trunc = (s: string | null | undefined, n = 42) => (!s ? "—" : s.length > n ? s.slice(0, n) + "…" : s);
