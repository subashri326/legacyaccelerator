export interface Me {
  username: string;
  email: string;
  role: string;
  permissions: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  username: string;
  role: string;
  permissions: string[];
}

export interface Client {
  id: string;
  slug: string;
  name: string;
  industry: string;
}

export interface ConnectorField {
  name: string;
  label: string;
  type: "string" | "integer" | "password" | "boolean" | "select";
  required: boolean;
  default: unknown;
  help: string;
  options: string[];
  secret: boolean;
}

export interface ConnectorType {
  id: string;
  name: string;
  kind: "source" | "destination" | "both";
  database: string;
  description: string;
  driver_extra: string;
  docs: string;
  fields: ConnectorField[];
}

export interface SavedConnection {
  id: string;
  name: string;
  client: string | null;
  kind: "source" | "target";
  type: string;
  type_name: string;
  settings: Record<string, unknown>;
  secret_fields_set: string[];
  last_test_status: "passed" | "failed" | null;
  last_test_message: string | null;
  last_tested_at: string | null;
  created_by: string;
}

export interface TestResult {
  ok: boolean;
  message: string;
  seconds: number;
}

export type StageName = "discover" | "assess" | "convert" | "move" | "reconcile";

export interface RunStage {
  name: StageName;
  status: "waiting" | "running" | "done" | "failed" | "skipped" | "interrupted";
  started_at?: string;
  finished_at?: string;
  summary?: Record<string, unknown>;
  error?: string;
}

export interface Run {
  id: string;
  project_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "interrupted";
  current_stage: StageName | null;
  stages: RunStage[];
  error: string | null;
  started_by: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface ConnectionRef {
  id: string | null;
  name: string;
  type: string;
}

export interface Migration {
  id: string;
  name: string;
  client: string;
  client_name: string;
  source_platform: string;
  target_platform: string | null;
  status: string;
  objects: number;
  by_state: Record<string, number>;
  blocked: number;
  last_run: Run | null;
  source: ConnectionRef | null;
  destination: ConnectionRef | null;
}

export interface Upload {
  path: string;
  name: string;
  size: number;
  tool: string;
}

export interface InventoryItem {
  object_key: string;
  name: string;
  kind: string;
  row_count?: number | null;
  is_dead_candidate?: boolean;
  state: string;
  blocker_reason?: string | null;
  complexity_score?: number | null;
  classification?: string | null;
  reasons: string[];
  effort_days?: Record<string, number>;
}

export interface ResidueItem {
  id: string;
  object_key: string;
  component_name: string;
  reason_code: string;
  reason_detail: string;
  original_snippet: string;
  ai_proposed_code: string | null;
  ai_rationale: string | null;
  ai_model: string | null;
  status: string;
}

export interface GeneratedArtifact {
  id: string;
  object_key: string;
  target_platform: string;
  artifact_kind: string;
  relative_path: string;
  compile_status: string;
  compile_log: string;
}

export interface ReconResult {
  id: string;
  recon_run_id: string;
  object_key: string;
  level: string;
  verdict: string;
  source_metrics: { row_count?: number } & Record<string, unknown>;
  target_metrics: { row_count?: number } & Record<string, unknown>;
  diff_summary: Record<string, unknown>;
}

export interface LoadRun {
  id: string;
  object_key: string;
  mover_plugin: string;
  mode: string;
  status: string;
  rows_loaded: number;
  partitions: { index: number; status: string; rows_loaded: number }[];
}

export interface Dashboard {
  project_id: string;
  total_objects: number;
  by_state: Record<string, number>;
  by_kind: Record<string, number>;
  blockers_count: number;
  blockers: { object_key: string; object_kind: string; state: string; blocker_reason: string }[];
}
