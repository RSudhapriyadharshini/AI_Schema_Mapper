export type Owner = "ETL" | "PROFESSIONAL" | "OTHER_SOURCE" | "SYSTEM";
export type Thresholds = { high_threshold: number; review_threshold: number };

export interface SchemaField {
  field: string; label: string; description: string; data_type: string;
  required: boolean; owner: Owner; source_priority: string[]; item_fields?: string[]; group?: string;
}
export interface CanonicalSchema { schema_version: string; owners: Owner[]; fields: SchemaField[] }

export interface Upload {
  id: number; file_name: string; format: string; num_records: number; num_fields: number;
  fields_extracted: number; dropped_empty: number; source_name: string; crawler_version: string; extraction_prompt_version: string;
  records: Record<string, unknown>[]; fields: { field: string; records: number }[];
}

export interface Run {
  id: number; label: string; created_at: string; finished_at: string | null; schema_version: string; model: string;
  prompt_version: string; source_file: string; status: "running" | "completed" | "failed";
  records_processed: number; total_records: number; fields_mapped: number; fields_unmapped: number; fields_ambiguous: number;
  crawler_version: string; extraction_prompt_version: string; thresholds: Thresholds; error_type: string | null; error_message: string | null;
  mapping_mode: string | null; llm_requests: number; input_tokens: number; output_tokens: number;
}
export interface Step { step_key: string; label: string; status: "pending" | "running" | "done" | "failed"; detail: string | null }

export interface Mapping {
  id: number; record_index: number; source_field: string; source_value: string; target_field: string | null;
  target_value: string | null; confidence: number; status: "mapped" | "unmapped" | "ambiguous"; owner: Owner | null;
  etl_can_populate: boolean; reason: string; llm_status: string; review_state: string | null;
  validation_note: string | null; band: string | null; origin: string | null;
}

export interface FieldCoverage {
  field: string; label: string; owner: Owner; required: boolean; source_fields: string[]; records_populated: number;
  records_total: number; fill_rate: number; value_available: boolean; etl_can_populate: boolean;
  status: "populated" | "partial" | "missing"; sample_value: string | null; source_priority: string[];
}
export interface Coverage {
  run: Run;
  metrics: Record<string, number>;
  fields: FieldCoverage[];
  ownership: { owner: Owner; total: number; populated: number; missing: number }[];
  missing: { field: string; label: string; owner: Owner; required: boolean; reason: string; recommended_action: string }[];
  reverse_mapping: { field: string; label: string; sources: { source_field: string; count: number }[] }[];
}
export interface Lineage {
  source_field: string; source_value: string; confidence: number; mapping_id: number | null; mapping_run: string;
  model: string; schema_version: string; prompt_version: string; review_state: string | null;
}
export interface CanonicalRecord {
  id: number; record_index: number; profile: Record<string, unknown>; flat: Record<string, unknown>; lineage: Record<string, Lineage>; raw: Record<string, unknown>;
}
export interface LlmCall {
  id: number; record_index: number; purpose: string | null; attempt: number; model: string; prompt_version: string; status: string;
  error: string | null; request_text: string; response_text: string | null; stop_reason: string | null;
  input_tokens: number | null; output_tokens: number | null; latency_ms: number | null; created_at: string;
}
export interface ApprovedExample { id: number; source_field: string; target_field: string; example_value: string | null }
export interface Health { ok: boolean; api_key_configured: boolean; model: string; prompt_version: string }
export interface SourceDecision {
  source_field: string; target_field: string | null; status: string; confidence: number; derived: boolean; human_approved?: boolean;
}
export interface SourceMapping {
  id: number; signature: string; fields: string[]; decisions: SourceDecision[]; model: string; schema_version: string;
  created_run_id: number; updated_at: string; times_reused: number;
}
export type MappingMode = "per_field" | "per_field_relearn" | "per_source" | "per_source_relearn" | "per_record";
export interface FieldDecision { id: number; source_name: string; path: string; decisions: SourceDecision[]; times_reused: number; model: string; created_run_id: number }
