import type {
  ApprovedExample, CanonicalPage, FieldDecision, MappingMode, MappingPage, ReviewQueue, SourceMapping, CanonicalSchema, Coverage, Health, LlmCall, Run, Step, Thresholds, Upload,
} from "../types";

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) { super(message); this.code = code; }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try { res = await fetch(`/api${path}`, init); }
  catch { throw new ApiError("NETWORK", "Cannot reach the backend on port 8000. Is it running?"); }
  if (!res.ok) {
    let body: any = null;
    try { body = await res.json(); } catch { /* not json */ }
    const d = body?.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) throw new ApiError(d.code ?? "ERROR", d.message ?? "Request failed");
    throw new ApiError("ERROR", typeof d === "string" ? d : `Request failed (${res.status})`);
  }
  return res.json();
}
const json = (method: string, body: unknown): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

export const api = {
  health: () => req<Health>("/health"),
  schema: () => req<CanonicalSchema>("/schema"),
  settings: () => req<Thresholds>("/settings"),
  saveSettings: (t: Thresholds) => req<Thresholds>("/settings", json("PUT", t)),
  uploadFile: (file: File, crawler: string, extraction: string, sourceName: string) => {
    const fd = new FormData();
    fd.append("file", file); fd.append("crawler_version", crawler); fd.append("extraction_prompt_version", extraction); fd.append("source_name", sourceName);
    return req<Upload>("/upload", { method: "POST", body: fd });
  },
  loadSample: () => req<Upload>("/upload/sample", { method: "POST" }),
  getUpload: (id: number) => req<Upload>(`/uploads/${id}`),
  listUploads: () => req<{ id: number }[]>("/uploads"),
  startRun: (upload_id: number, use_approved_examples: boolean, mapping_mode: MappingMode, strict_no_llm: boolean) =>
    req<{ run_id: number }>("/runs", json("POST", { upload_id, use_approved_examples, mapping_mode, strict_no_llm })),
  fieldDecisions: () => req<FieldDecision[]>("/field-decisions"),
  forgetFieldDecisions: (source: string) => req<{ ok: boolean }>(`/field-decisions?source_name=${encodeURIComponent(source)}`, { method: "DELETE" }),
  sourceMappings: () => req<SourceMapping[]>("/source-mappings"),
  deleteSourceMapping: (id: number) => req<{ ok: boolean }>(`/source-mappings/${id}`, { method: "DELETE" }),
  runs: () => req<Run[]>("/runs"),
  runStatus: (id: number) => req<{ run: Run; steps: Step[] }>(`/runs/${id}/status`),
  mappings: (id: number, o: { record?: number; status?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams();
    Object.entries(o).forEach(([k, v]) => v !== undefined && q.set(k, String(v)));
    return req<MappingPage>(`/runs/${id}/mappings?${q}`);
  },
  reviewQueue: (id: number, includeUnmapped: boolean) => req<ReviewQueue>(`/runs/${id}/review-queue?include_unmapped=${includeUnmapped}`),
  canonical: (id: number, limit = 30, offset = 0) => req<CanonicalPage>(`/runs/${id}/canonical?limit=${limit}&offset=${offset}`),
  coverage: (id: number) => req<Coverage>(`/runs/${id}/coverage`),
  logs: (id: number) => req<{ run: Run; calls: LlmCall[] }>(`/runs/${id}/logs`),
  review: (mappingId: number, action: "approve" | "reject" | "skip", target_field?: string, scope: "record" | "all" = "record") =>
    req<{ ok: boolean; updated: number }>(`/mappings/${mappingId}/review`, json("POST", { action, target_field, scope })),
  examples: () => req<ApprovedExample[]>("/approved-examples"),
  seedExamples: () => req<ApprovedExample[]>("/approved-examples/seed", { method: "POST" }),
  deleteExample: (id: number) => req<{ ok: boolean }>(`/approved-examples/${id}`, { method: "DELETE" }),
  table: (table: string, runId?: number) =>
    req<{ counts: Record<string, number>; rows: Record<string, unknown>[] }>(`/db/${table}${runId ? `?run_id=${runId}` : ""}`),
};
