import type {
  DashboardData,
  GeneratedArtifact,
  InventoryItem,
  PluginEntry,
  ProjectSummary,
  ReconResult,
  ResidueItem,
  TokenResponse,
} from "../types";

const BASE = "/api/v1";

class ApiClient {
  private token: string | null = localStorage.getItem("l2c_token");

  setToken(token: string | null): void {
    this.token = token;
    if (token) {
      localStorage.setItem("l2c_token", token);
    } else {
      localStorage.removeItem("l2c_token");
    }
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(init.headers as Record<string, string> | undefined),
    };
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
    }
    const resp = await fetch(`${BASE}${path}`, { ...init, headers });
    if (!resp.ok) {
      const errBody = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(errBody.detail || `HTTP ${resp.status}`);
    }
    return (await resp.json()) as T;
  }

  login(username: string, password: string): Promise<TokenResponse> {
    return this.request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  }

  listProjects(): Promise<ProjectSummary[]> {
    return this.request<ProjectSummary[]>("/projects");
  }

  registerProjectYaml(rawYaml: string): Promise<{ project_id: string }> {
    return this.request("/projects/register-yaml", {
      method: "POST",
      body: JSON.stringify({ raw_yaml: rawYaml }),
    });
  }

  runStage(projectId: string, stage: string, body: Record<string, unknown> = {}): Promise<unknown> {
    return this.request(`/projects/${projectId}/${stage}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getDashboard(projectId: string): Promise<DashboardData> {
    return this.request<DashboardData>(`/projects/${projectId}/dashboard`);
  }

  getInventory(projectId: string): Promise<{ items: InventoryItem[] }> {
    return this.request<{ items: InventoryItem[] }>(`/projects/${projectId}/inventory`);
  }

  listResidues(projectId: string): Promise<ResidueItem[]> {
    return this.request<ResidueItem[]>(`/projects/${projectId}/residues`);
  }

  aiProposeResidue(projectId: string, residueId: string): Promise<ResidueItem> {
    return this.request<ResidueItem>(`/projects/${projectId}/residues/${residueId}/ai-propose`, {
      method: "POST",
    });
  }

  decideResidue(
    projectId: string,
    residueId: string,
    decision: "approved" | "rejected",
    replacementCode?: string
  ): Promise<unknown> {
    return this.request(`/projects/${projectId}/residues/${residueId}/decide`, {
      method: "POST",
      body: JSON.stringify({ decision, replacement_code: replacementCode, comment: "Reviewed in React UI" }),
    });
  }

  listArtifacts(projectId: string): Promise<GeneratedArtifact[]> {
    return this.request<GeneratedArtifact[]>(`/projects/${projectId}/artifacts`);
  }

  getArtifactContent(projectId: string, artifactId: string): Promise<{ content: string }> {
    return this.request<{ content: string }>(`/projects/${projectId}/artifacts/${artifactId}/content`);
  }

  listReconciliation(projectId: string): Promise<ReconResult[]> {
    return this.request<ReconResult[]>(`/projects/${projectId}/reconciliation`);
  }

  signOff(projectId: string, objectKey: string): Promise<unknown> {
    return this.request(`/projects/${projectId}/signoff`, {
      method: "POST",
      body: JSON.stringify({ object_key: objectKey }),
    });
  }

  listPlugins(): Promise<PluginEntry[]> {
    return this.request<PluginEntry[]>("/plugins");
  }

  runGoldenTests(): Promise<Array<{ name: string; passed: boolean; message: string }>> {
    return this.request("/plugins/run-golden", { method: "POST" });
  }
}

export const api = new ApiClient();
