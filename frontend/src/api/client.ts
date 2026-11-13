import type {
  Client,
  ConnectorType,
  Dashboard,
  GeneratedArtifact,
  InventoryItem,
  LoadRun,
  Me,
  Migration,
  ReconResult,
  ResidueItem,
  Run,
  SavedConnection,
  StageName,
  TestResult,
  TokenResponse,
  Upload,
} from "../types";

const BASE = "/api/v1";
const TOKEN_KEY = "l2c_token";

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

class ApiClient {
  private token: string | null = readToken();
  onUnauthorized: (() => void) | null = null;

  get signedIn(): boolean {
    return this.token !== null;
  }

  setToken(token: string | null): void {
    this.token = token;
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* storage unavailable: keep the token in memory only */
    }
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
    if (!(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    const resp = await fetch(`${BASE}${path}`, { ...init, headers });
    if (resp.status === 401) {
      this.setToken(null);
      this.onUnauthorized?.();
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({ detail: resp.statusText }));
      const detail = Array.isArray(body.detail)
        ? body.detail.map((d: { msg: string }) => d.msg).join("; ")
        : body.detail;
      throw new ApiError(detail || `Request failed (${resp.status})`, resp.status);
    }
    return (await resp.json()) as T;
  }

  private post<T>(path: string, body: unknown = {}): Promise<T> {
    return this.request<T>(path, { method: "POST", body: JSON.stringify(body) });
  }

  // auth
  login = (username: string, password: string) =>
    this.post<TokenResponse>("/auth/login", { username, password });
  me = () => this.request<Me>("/auth/me");

  // clients & connections
  connectorTypes = () => this.request<ConnectorType[]>("/connector-types");
  clients = () => this.request<Client[]>("/clients");
  createClient = (name: string) => this.post<Client>("/clients", { name });
  connections = (client: string, kind?: "source" | "target") =>
    this.request<SavedConnection[]>(
      `/connections?client=${encodeURIComponent(client)}${kind ? `&kind=${kind}` : ""}`,
    );
  connection = async (client: string, id: string) => {
    const all = await this.connections(client);
    const found = all.find((c) => c.id === id);
    if (!found) throw new ApiError("Connection not found", 404);
    return found;
  };
  testUnsaved = (type: string, settings: Record<string, unknown>, connectionId?: string) =>
    this.post<TestResult>("/connections/test", {
      type,
      settings,
      connection_id: connectionId ?? null,
    });
  createConnection = (body: {
    client: string;
    name: string;
    type: string;
    kind?: string;
    settings: Record<string, unknown>;
  }) => this.post<SavedConnection>("/connections", body);
  updateConnection = (id: string, body: { name?: string; settings: Record<string, unknown> }) =>
    this.request<SavedConnection>(`/connections/${id}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
  deleteConnection = (id: string) =>
    this.request<{ deleted: string }>(`/connections/${id}`, { method: "DELETE" });
  testSaved = (id: string) => this.post<TestResult>(`/connections/${id}/test`);
  schemas = (id: string) => this.request<{ schemas: string[] }>(`/connections/${id}/schemas`);
  tables = (id: string, schema: string) =>
    this.request<{ schema: string; tables: string[] }>(
      `/connections/${id}/schemas?schema=${encodeURIComponent(schema)}`,
    );

  upload = (client: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return this.request<Upload>(`/clients/${encodeURIComponent(client)}/uploads`, {
      method: "POST",
      body: form,
    });
  };

  // migrations & runs
  migrations = () => this.request<Migration[]>("/migrations");
  migration = (id: string) => this.request<Migration>(`/migrations/${id}`);
  createMigration = (body: Record<string, unknown>) =>
    this.post<{ id: string; name: string; config_yaml: string }>("/migrations", body);
  startRun = (projectId: string, stages?: StageName[]) =>
    this.post<Run>(`/projects/${projectId}/runs`, stages ? { stages } : {});
  runs = (projectId: string) => this.request<Run[]>(`/projects/${projectId}/runs`);
  run = (projectId: string, runId: string) =>
    this.request<Run>(`/projects/${projectId}/runs/${runId}`);

  // project details
  project = (id: string) =>
    this.request<{ configs: { version_num: number; raw_yaml: string; is_active: boolean }[] }>(
      `/projects/${id}`,
    );
  dashboard = (id: string) => this.request<Dashboard>(`/projects/${id}/dashboard`);
  inventory = (id: string) => this.request<{ items: InventoryItem[] }>(`/projects/${id}/inventory`);
  residues = (id: string) => this.request<ResidueItem[]>(`/projects/${id}/residues`);
  decide = (id: string, residueId: string, decision: "approved" | "rejected", code: string) =>
    this.post(`/projects/${id}/residues/${residueId}/decide`, {
      decision,
      replacement_code: code,
      comment: "",
    });
  aiPropose = (id: string, residueId: string) =>
    this.post<ResidueItem>(`/projects/${id}/residues/${residueId}/ai-propose`);
  artifacts = (id: string) => this.request<GeneratedArtifact[]>(`/projects/${id}/artifacts`);
  artifactContent = (id: string, artifactId: string) =>
    this.request<{ content: string }>(`/projects/${id}/artifacts/${artifactId}/content`);
  reconciliation = (id: string) => this.request<ReconResult[]>(`/projects/${id}/reconciliation`);
  loadRuns = (id: string) => this.request<LoadRun[]>(`/projects/${id}/load-runs`);
  signoff = (id: string, objectKey: string) =>
    this.post(`/projects/${id}/signoff`, { object_key: objectKey });
}

export const api = new ApiClient();
