import React, { useEffect, useState } from "react";
import { api } from "./api/client";
import type {
  DashboardData,
  GeneratedArtifact,
  InventoryItem,
  PluginEntry,
  ProjectSummary,
  ReconResult,
  ResidueItem,
  TokenResponse,
} from "./types";

type Page = "track" | "assess" | "convert" | "move_recon" | "signoff" | "admin";

export function App(): React.ReactElement {
  const [auth, setAuth] = useState<TokenResponse | null>(null);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin123!");
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>("track");

  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState<string>("");
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [inventory, setInventory] = useState<InventoryItem[]>([]);
  const [residues, setResidues] = useState<ResidueItem[]>([]);
  const [artifacts, setArtifacts] = useState<GeneratedArtifact[]>([]);
  const [artifactPreview, setArtifactPreview] = useState<string>("");
  const [recons, setRecons] = useState<ReconResult[]>([]);
  const [plugins, setPlugins] = useState<PluginEntry[]>([]);
  const [yamlInput, setYamlInput] = useState("");

  async function loadProjectData(pid: string): Promise<void> {
    if (!pid) return;
    const [d, inv, res, arts, rec] = await Promise.all([
      api.getDashboard(pid),
      api.getInventory(pid),
      api.listResidues(pid),
      api.listArtifacts(pid),
      api.listReconciliation(pid),
    ]);
    setDashboard(d);
    setInventory(inv.items);
    setResidues(res);
    setArtifacts(arts);
    setRecons(rec);
  }

  async function handleLogin(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    setError(null);
    try {
      const tok = await api.login(username, password);
      api.setToken(tok.access_token);
      setAuth(tok);
      const list = await api.listProjects();
      setProjects(list);
      if (list.length > 0) {
        setProjectId(list[0].id);
        await loadProjectData(list[0].id);
      }
      setPlugins(await api.listPlugins());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    }
  }

  useEffect(() => {
    if (auth && projectId) {
      void loadProjectData(projectId);
    }
  }, [auth, projectId]);

  if (!auth) {
    return (
      <form className="login-box" onSubmit={handleLogin}>
        <h2>L2C Accelerator Sign In</h2>
        <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" />
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" />
        {error && <div style={{ color: "#dc2626" }}>{error}</div>}
        <button className="primary" type="submit">Sign In</button>
      </form>
    );
  }

  const platformTotals = inventory.reduce<Record<string, number>>((acc, item) => {
    for (const [k, v] of Object.entries(item.effort_days || {})) {
      acc[k] = Number(((acc[k] || 0) + v).toFixed(2));
    }
    return acc;
  }, {});

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>L2C Accelerator</h1>
        <div style={{ fontSize: "0.82rem", color: "#94a3b8" }}>
          {auth.username} ({auth.role})
        </div>
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.target_platform})
            </option>
          ))}
        </select>
        <button className={`nav-btn ${page === "track" ? "active" : ""}`} onClick={() => setPage("track")}>
          Track &amp; Control Tower
        </button>
        <button className={`nav-btn ${page === "assess" ? "active" : ""}`} onClick={() => setPage("assess")}>
          Discover &amp; Assess
        </button>
        <button className={`nav-btn ${page === "convert" ? "active" : ""}`} onClick={() => setPage("convert")}>
          Convert &amp; Review Queue ({residues.filter((r) => r.status === "open" || r.status === "ai_proposed").length})
        </button>
        <button className={`nav-btn ${page === "move_recon" ? "active" : ""}`} onClick={() => setPage("move_recon")}>
          Move &amp; Reconcile
        </button>
        <button className={`nav-btn ${page === "signoff" ? "active" : ""}`} onClick={() => setPage("signoff")}>
          Client Sign-Off
        </button>
        <button className={`nav-btn ${page === "admin" ? "active" : ""}`} onClick={() => setPage("admin")}>
          Admin &amp; Plugins
        </button>
      </aside>

      <main className="main">
        {error && <div className="card" style={{ borderColor: "#ef4444", color: "#b91c1c" }}>{error}</div>}

        {page === "track" && dashboard && (
          <>
            <h2>Control Tower &amp; Lifecycle Funnel</h2>
            <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
              <button
                className="primary"
                onClick={async () => {
                  await api.runStage(projectId, "run-pipeline");
                  await loadProjectData(projectId);
                }}
              >
                Run End-to-End Pipeline
              </button>
            </div>
            <div className="grid-cards">
              <div className="card">
                <div>Total Objects</div>
                <div className="metric-val">{dashboard.total_objects}</div>
              </div>
              <div className="card">
                <div>Validated</div>
                <div className="metric-val">{dashboard.by_state.Validated || 0}</div>
              </div>
              <div className="card">
                <div>Signed Off</div>
                <div className="metric-val">{dashboard.by_state.SignedOff || 0}</div>
              </div>
              <div className="card">
                <div>Active Blockers</div>
                <div className="metric-val">{dashboard.blockers_count}</div>
              </div>
            </div>
            <h3>Wave Progress</h3>
            <table>
              <thead>
                <tr><th>Wave</th><th>Total</th><th>States</th></tr>
              </thead>
              <tbody>
                {dashboard.waves.map((w) => (
                  <tr key={w.wave_id}>
                    <td>{w.name}</td>
                    <td>{w.total_objects}</td>
                    <td>{JSON.stringify(w.states)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {page === "assess" && (
          <>
            <h2>Discover &amp; Side-by-Side Platform Assessment</h2>
            <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
              <button className="primary" onClick={async () => { await api.runStage(projectId, "discover"); await loadProjectData(projectId); }}>
                Run Discover
              </button>
              <button className="primary" onClick={async () => { await api.runStage(projectId, "assess"); await loadProjectData(projectId); }}>
                Run Assess
              </button>
            </div>
            <div className="grid-cards">
              {Object.entries(platformTotals).map(([platform, days]) => (
                <div key={platform} className="card">
                  <div>{platform.toUpperCase()} Effort</div>
                  <div className="metric-val">{days} d</div>
                </div>
              ))}
            </div>
            <table>
              <thead>
                <tr><th>Object</th><th>Kind</th><th>State</th><th>Classification</th><th>Score</th><th>Reasons</th></tr>
              </thead>
              <tbody>
                {inventory.map((item) => (
                  <tr key={item.object_key}>
                    <td>{item.object_key}</td>
                    <td>{item.kind}</td>
                    <td><span className="badge">{item.state}</span></td>
                    <td>{item.classification || "-"}</td>
                    <td>{item.complexity_score ?? "-"}</td>
                    <td>{item.reasons.join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {page === "convert" && (
          <>
            <h2>Convert &amp; Residue Review Queue</h2>
            <button
              className="primary"
              onClick={async () => {
                await api.runStage(projectId, "convert", { deliver_git: true });
                await loadProjectData(projectId);
              }}
            >
              Run Convert + Git Wave PR
            </button>
            <h3>Flagged Residue Items</h3>
            {residues.map((r) => (
              <div key={r.id} className="card" style={{ marginBottom: 12 }}>
                <strong>{r.object_key}</strong> :: {r.component_name} <span className="badge">{r.status}</span>
                <p>{r.reason_detail}</p>
                <pre className="code">{r.original_snippet}</pre>
                {r.ai_proposed_code && <pre className="code">{r.ai_proposed_code}</pre>}
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="secondary" onClick={async () => { await api.aiProposeResidue(projectId, r.id); await loadProjectData(projectId); }}>
                    Propose AI Fix
                  </button>
                  <button className="primary" onClick={async () => { await api.decideResidue(projectId, r.id, "approved", r.ai_proposed_code || undefined); await loadProjectData(projectId); }}>
                    Approve
                  </button>
                  <button className="secondary" onClick={async () => { await api.decideResidue(projectId, r.id, "rejected"); await loadProjectData(projectId); }}>
                    Reject
                  </button>
                </div>
              </div>
            ))}
            <h3>Generated Artifacts</h3>
            <table>
              <thead><tr><th>Path</th><th>Kind</th><th>Compile</th><th>Action</th></tr></thead>
              <tbody>
                {artifacts.map((a) => (
                  <tr key={a.id}>
                    <td>{a.relative_path}</td>
                    <td>{a.artifact_kind}</td>
                    <td>{a.compile_status}</td>
                    <td>
                      <button
                        className="secondary"
                        onClick={async () => {
                          const res = await api.getArtifactContent(projectId, a.id);
                          setArtifactPreview(res.content);
                        }}
                      >
                        Inspect
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {artifactPreview && <pre className="code">{artifactPreview}</pre>}
          </>
        )}

        {page === "move_recon" && (
          <>
            <h2>Move Data &amp; Pushed-Down Reconciliation</h2>
            <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
              <button className="primary" onClick={async () => { await api.runStage(projectId, "move", { mode: "bulk" }); await loadProjectData(projectId); }}>
                Run Bulk Move
              </button>
              <button className="primary" onClick={async () => { await api.runStage(projectId, "reconcile"); await loadProjectData(projectId); }}>
                Run Reconcile
              </button>
            </div>
            <table>
              <thead><tr><th>Object</th><th>Level</th><th>Verdict</th><th>Evidence URI</th></tr></thead>
              <tbody>
                {recons.map((r) => (
                  <tr key={r.id}>
                    <td>{r.object_key}</td>
                    <td>{r.level}</td>
                    <td><span className="badge">{r.verdict}</span></td>
                    <td>{r.evidence_uri}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        {page === "signoff" && (
          <>
            <h2>Client Data Owner Sign-Off</h2>
            <table>
              <thead><tr><th>Object</th><th>State</th><th>Evidence</th><th>Action</th></tr></thead>
              <tbody>
                {inventory
                  .filter((i) => i.state === "Validated" || i.state === "SignedOff")
                  .map((i) => (
                    <tr key={i.object_key}>
                      <td>{i.object_key}</td>
                      <td><span className="badge">{i.state}</span></td>
                      <td>{i.reasons.join(", ") || "Recon Verified"}</td>
                      <td>
                        {i.state === "Validated" ? (
                          <button
                            className="primary"
                            onClick={async () => {
                              await api.signOff(projectId, i.object_key);
                              await loadProjectData(projectId);
                            }}
                          >
                            Sign Off
                          </button>
                        ) : (
                          "Signed Off"
                        )}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </>
        )}

        {page === "admin" && (
          <>
            <h2>Platform Admin · Plugins &amp; Config Registration</h2>
            <button
              className="primary"
              onClick={async () => {
                await api.runGoldenTests();
                setPlugins(await api.listPlugins());
              }}
            >
              Run Plugin Golden Checks
            </button>
            <table>
              <thead><tr><th>Type</th><th>Name</th><th>Version</th><th>Golden Status</th></tr></thead>
              <tbody>
                {plugins.map((p) => (
                  <tr key={p.name}>
                    <td>{p.plugin_type}</td>
                    <td>{p.name}</td>
                    <td>{p.version}</td>
                    <td><span className="badge">{p.last_golden_status}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="card" style={{ marginTop: 20 }}>
              <h3>Register New Client YAML</h3>
              <textarea
                rows={8}
                style={{ width: "100%" }}
                value={yamlInput}
                onChange={(e) => setYamlInput(e.target.value)}
                placeholder="Paste client YAML configuration..."
              />
              <button
                className="primary"
                style={{ marginTop: 8 }}
                onClick={async () => {
                  const res = await api.registerProjectYaml(yamlInput);
                  const list = await api.listProjects();
                  setProjects(list);
                  setProjectId(res.project_id);
                }}
              >
                Register Client Config
              </button>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
