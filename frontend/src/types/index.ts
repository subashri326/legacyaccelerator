export interface TokenResponse {
  access_token: string;
  token_type: string;
  username: string;
  role: string;
  permissions: string[];
}

export interface ProjectSummary {
  id: string;
  slug: string;
  name: string;
  client_slug: string;
  source_platform: string;
  etl_tool: string;
  target_platform: string;
  status: string;
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
  effort_days: Record<string, number>;
}

export interface DashboardData {
  project_id: string;
  total_objects: number;
  by_state: Record<string, number>;
  by_kind: Record<string, number>;
  blockers_count: number;
  blockers: Array<{
    object_key: string;
    object_kind: string;
    state: string;
    owner: string;
    blocker_reason: string;
  }>;
  waves: Array<{
    wave_id: string;
    wave_number: number;
    name: string;
    total_objects: number;
    states: Record<string, number>;
  }>;
}

export interface ResidueItem {
  id: string;
  object_key: string;
  component_name: string;
  reason_code: string;
  reason_detail: string;
  original_snippet: string;
  ai_proposed_code?: string | null;
  ai_rationale?: string | null;
  ai_model?: string | null;
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
  source_metrics: Record<string, unknown>;
  target_metrics: Record<string, unknown>;
  diff_summary: Record<string, unknown>;
  evidence_uri: string;
  created_at: string;
}

export interface PluginEntry {
  name: string;
  plugin_type: string;
  version: string;
  capabilities: Record<string, unknown>;
  enabled: boolean;
  last_golden_status: string;
}
