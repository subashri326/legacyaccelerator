import {
  ArrowRight,
  Calculator,
  CheckCircle2,
  Circle,
  Code2,
  Database,
  Loader2,
  MinusCircle,
  Play,
  ScanSearch,
  Sparkles,
  Truck,
  XCircle,
} from "lucide-react";
import React, { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import {
  Badge,
  ConnectorIcon,
  Empty,
  Notice,
  PageHead,
  Spinner,
  StatusBadge,
  duration,
  fmt,
  timeAgo,
  useLoad,
  usePoll,
} from "../components/ui";
import { useApp } from "../context";
import type { Migration, ResidueItem, Run, RunStage, StageName } from "../types";

/* ---------- plain-language stage descriptions ---------- */
const STAGE_INFO: Record<StageName, { title: string; doing: string; icon: React.ElementType }> = {
  discover: { title: "Read the old system", doing: "Reading tables, views and jobs…", icon: ScanSearch },
  assess: { title: "Estimate effort", doing: "Scoring how hard each object is…", icon: Calculator },
  convert: { title: "Write new code", doing: "Translating to the new platform…", icon: Code2 },
  move: { title: "Copy the data", doing: "Creating tables and copying rows…", icon: Truck },
  reconcile: { title: "Check the numbers", doing: "Comparing both sides…", icon: CheckCircle2 },
};
const ORDER: StageName[] = ["discover", "assess", "convert", "move", "reconcile"];

const PLATFORM: Record<string, string> = { snowflake: "Snowflake", databricks: "Databricks", fabric: "Fabric" };
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function num(s: Record<string, unknown> | undefined, k: string): number {
  const v = s?.[k];
  return typeof v === "number" ? v : Array.isArray(v) ? v.length : 0;
}

function stageResult(stage: RunStage, platform: string | null): React.ReactNode {
  const s = stage.summary;
  if (!s) return null;
  switch (stage.name) {
    case "discover":
      return `${plural(num(s, "tables_discovered"), "table")}, ${plural(num(s, "views_discovered"), "view")}, ${plural(num(s, "etl_jobs_discovered"), "job")} found`;
    case "assess": {
      const eff = (s.effort_by_platform ?? {}) as Record<string, number>;
      const mine = platform && eff[platform] != null ? eff[platform] : null;
      return mine != null
        ? `About ${mine} days of work on ${PLATFORM[platform!] ?? platform}`
        : Object.entries(eff)
            .map(([p, d]) => `${PLATFORM[p] ?? p} ${d}d`)
            .join(" · ");
    }
    case "convert":
      return `${plural(num(s, "files_generated"), "file")} written · ${num(s, "residue_items_opened")} for a person to review`;
    case "move": {
      const held = num(s, "held_tables");
      const failed = num(s, "failed_tables");
      return (
        <>
          {plural(num(s, "tables_loaded"), "table")}, {fmt(num(s, "total_rows_loaded"))} rows copied
          {held > 0 && <div className="faint">{held} waiting for review</div>}
          {failed > 0 && <div className="sresult err">{failed} failed</div>}
        </>
      );
    }
    case "reconcile": {
      const ok = num(s, "passed") + num(s, "passed_within_tolerance");
      const bad = num(s, "failed");
      return (
        <>
          {ok} of {num(s, "tables_checked")} tables match
          {bad > 0 && <div className="sresult err">{bad} don't match</div>}
        </>
      );
    }
  }
}

function StageIcon({ stage }: { stage: RunStage }) {
  const Info = STAGE_INFO[stage.name].icon;
  if (stage.status === "running") return <Loader2 size={18} className="spin" />;
  if (stage.status === "done") return <CheckCircle2 size={18} />;
  if (stage.status === "failed" || stage.status === "interrupted") return <XCircle size={18} />;
  if (stage.status === "skipped") return <MinusCircle size={18} />;
  return <Info size={18} />;
}

function Timeline({ run, platform }: { run: Run | null; platform: string | null }) {
  const stages: RunStage[] = ORDER.map(
    (n) => run?.stages.find((s) => s.name === n) ?? { name: n, status: "waiting" },
  );
  return (
    <div className="timeline">
      {stages.map((st) => (
        <div className={`step ${st.status === "interrupted" ? "failed" : st.status}`} key={st.name}>
          <div className="icon">
            <StageIcon stage={st} />
          </div>
          <div className="sname">{STAGE_INFO[st.name].title}</div>
          <div className="sresult">
            {st.status === "running" ? (
              <span className="pulse muted">{STAGE_INFO[st.name].doing}</span>
            ) : st.status === "failed" || st.status === "interrupted" ? (
              <span className="sresult err">{st.error ?? "Stopped"}</span>
            ) : st.status === "skipped" ? (
              <span className="faint">Skipped</span>
            ) : st.status === "waiting" ? (
              <span className="faint">Waiting</span>
            ) : (
              stageResult(st, platform)
            )}
          </div>
          {st.started_at && <div className="stime">{duration(st.started_at, st.finished_at)}</div>}
        </div>
      ))}
    </div>
  );
}

/* ---------- tabs ---------- */
const STATE_ORDER = ["Discovered", "Assessed", "Converted", "Reviewed", "Deployed", "Loaded", "Validated", "SignedOff"];
const STATE_COLOR: Record<string, string> = {
  Discovered: "var(--text-3)",
  Assessed: "var(--warn)",
  Converted: "var(--info)",
  Reviewed: "var(--info)",
  Deployed: "var(--accent)",
  Loaded: "var(--accent)",
  Validated: "var(--ok)",
  SignedOff: "var(--ok)",
};

function Progress({ m }: { m: Migration }) {
  const total = Math.max(m.objects, 1);
  return (
    <div className="card card-pad">
      <div className="row" style={{ marginBottom: 10 }}>
        <strong>Where every object stands</strong>
        <div className="spacer" />
        {m.blocked > 0 && <Badge tone="warn">{m.blocked} blocked</Badge>}
      </div>
      <div className="bar">
        {STATE_ORDER.map((s) =>
          m.by_state[s] ? (
            <span key={s} title={`${s}: ${m.by_state[s]}`} style={{ width: `${(100 * m.by_state[s]) / total}%`, background: STATE_COLOR[s] }} />
          ) : null,
        )}
      </div>
      <div className="legend">
        {STATE_ORDER.filter((s) => m.by_state[s]).map((s) => (
          <span key={s}>
            <i style={{ background: STATE_COLOR[s] }} />
            {s === "SignedOff" ? "Signed off" : s} {m.by_state[s]}
          </span>
        ))}
      </div>
    </div>
  );
}

function ObjectsTab({ id }: { id: string }) {
  const { data, error } = useLoad(() => api.inventory(id), [id]);
  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!data) return <Spinner />;
  if (!data.items.length) return <Empty title="Nothing discovered yet">Run the migration to read the old system.</Empty>;
  return (
    <div className="card">
      <table className="list">
        <thead>
          <tr>
            <th>Object</th>
            <th>Kind</th>
            <th>Rows</th>
            <th>Can it convert itself?</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((o) => (
            <tr key={o.object_key}>
              <td>
                <code>{o.name}</code>
                {o.blocker_reason && <div className="faint">{o.blocker_reason}</div>}
              </td>
              <td className="muted">{o.kind.replace(/_/g, " ").toLowerCase()}</td>
              <td>{o.row_count != null ? fmt(o.row_count) : "—"}</td>
              <td>
                {o.classification ? (
                  <Badge tone={o.classification === "auto_convertible" ? "ok" : o.classification === "manual" ? "bad" : "warn"}>
                    {({ auto_convertible: "Automatic", needs_review: "Needs a review", manual: "Manual" } as Record<string, string>)[o.classification] ?? o.classification}
                  </Badge>
                ) : (
                  "—"
                )}
              </td>
              <td>
                <Badge tone={o.state === "Validated" || o.state === "SignedOff" ? "ok" : "neutral"}>{o.state}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ResidueCard({ id, r, onDone }: { id: string; r: ResidueItem; onDone: () => void }) {
  const { can } = useApp();
  const [code, setCode] = useState(r.ai_proposed_code ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const open = r.status === "open" || r.status === "ai_proposed";
  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="residue card" open={open}>
      <summary className="card-pad row">
        <strong>{r.component_name}</strong>
        <span className="faint">{r.object_key}</span>
        <div className="spacer" />
        <Badge tone={r.status === "approved" ? "ok" : r.status === "rejected" ? "bad" : "warn"}>{r.status.replace("_", " ")}</Badge>
      </summary>
      <div className="card-pad stack" style={{ paddingTop: 0 }}>
        <div>
          <Badge tone="info">{r.reason_code.replace(/_/g, " ").toLowerCase()}</Badge> <span className="muted">{r.reason_detail}</span>
        </div>
        <div>
          <div className="faint">Original code</div>
          <pre className="code">{r.original_snippet || "(none)"}</pre>
        </div>
        {r.ai_rationale && (
          <Notice tone="info">
            <strong>AI suggestion:</strong> {r.ai_rationale}
          </Notice>
        )}
        {open && can("review_residue") && (
          <>
            <div className="field">
              <label>Replacement code (leave empty if it isn't needed on the new platform)</label>
              <textarea rows={6} value={code} onChange={(e) => setCode(e.target.value)} style={{ fontFamily: "var(--mono)" }} />
            </div>
            {err && <Notice tone="bad">{err}</Notice>}
            <div className="row">
              <button className="btn" disabled={busy} onClick={() => void act(async () => setCode((await api.aiPropose(id, r.id)).ai_proposed_code ?? ""))}>
                <Sparkles size={14} /> Ask AI for a draft
              </button>
              <div className="spacer" />
              <button className="btn danger" disabled={busy} onClick={() => void act(() => api.decide(id, r.id, "rejected", code))}>
                Reject
              </button>
              <button className="btn primary" disabled={busy} onClick={() => void act(() => api.decide(id, r.id, "approved", code))}>
                Approve
              </button>
            </div>
          </>
        )}
      </div>
    </details>
  );
}

function ReviewTab({ id, onChange }: { id: string; onChange: () => void }) {
  const { data, error, reload } = useLoad(() => api.residues(id), [id]);
  const [show, setShow] = useState<"open" | "all">("open");
  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!data) return <Spinner />;
  const list = data.filter((r) => show === "all" || r.status === "open" || r.status === "ai_proposed");
  return (
    <div className="stack">
      <div className="row">
        <span className="muted">
          These are the pieces the accelerator could not translate with certainty. A person decides each one; approved
          code is put into the generated files on the next run.
        </span>
        <div className="spacer" />
        <select value={show} onChange={(e) => setShow(e.target.value as "open" | "all")} style={{ width: "auto" }}>
          <option value="open">Open only</option>
          <option value="all">All</option>
        </select>
      </div>
      {!list.length ? (
        <div className="card">
          <Empty title="Nothing waiting for review">Everything was translated automatically or has been decided.</Empty>
        </div>
      ) : (
        list.map((r) => (
          <ResidueCard
            key={r.id}
            id={id}
            r={r}
            onDone={() => {
              void reload();
              onChange();
            }}
          />
        ))
      )}
    </div>
  );
}

function CodeTab({ id }: { id: string }) {
  const { data, error } = useLoad(() => api.artifacts(id), [id]);
  const [sel, setSel] = useState<string | null>(null);
  const [content, setContent] = useState<string>("");
  useEffect(() => {
    if (!sel) return;
    setContent("Loading…");
    api.artifactContent(id, sel).then(
      (r) => setContent(r.content),
      (e: Error) => setContent(e.message),
    );
  }, [id, sel]);
  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!data) return <Spinner />;
  if (!data.length) return <Empty title="No code written yet">The "Write new code" step creates it.</Empty>;
  const current = data.find((a) => a.id === sel);
  return (
    <div className="split">
      <div className="card filelist">
        {data.map((a) => (
          <button key={a.id} className={a.id === sel ? "on" : ""} title={a.relative_path} onClick={() => setSel(a.id)}>
            {a.compile_status === "failed" ? "✗ " : ""}
            {a.relative_path}
          </button>
        ))}
      </div>
      <div className="card card-pad stack">
        {current ? (
          <>
            <div className="row">
              <code>{current.relative_path}</code>
              <div className="spacer" />
              <StatusBadge status={current.compile_status === "passed" ? "passed" : current.compile_status} />
            </div>
            <pre className="code" style={{ maxHeight: 520 }}>{content}</pre>
            {current.compile_status === "failed" && current.compile_log && <Notice tone="bad">{current.compile_log}</Notice>}
          </>
        ) : (
          <span className="muted">Pick a file on the left.</span>
        )}
      </div>
    </div>
  );
}

function DataTab({ id }: { id: string }) {
  const recon = useLoad(() => api.reconciliation(id), [id]);
  const loads = useLoad(() => api.loadRuns(id), [id]);
  if (recon.error) return <Notice tone="bad">{recon.error}</Notice>;
  if (!recon.data || !loads.data) return <Spinner />;
  const rows = new Map<string, { rowsCopied?: number; load?: string; verdict?: string; src?: number; tgt?: number; diff?: Record<string, unknown> }>();
  // Newest first: keep each table's latest copy attempt.
  for (const l of loads.data)
    if (!rows.has(l.object_key)) rows.set(l.object_key, { rowsCopied: l.rows_loaded, load: l.status });
  const seen = new Set<string>();
  for (const r of recon.data) {
    if (seen.has(r.object_key)) continue; // newest first: keep each table's latest check
    seen.add(r.object_key);
    rows.set(r.object_key, {
      ...rows.get(r.object_key),
      verdict: r.verdict,
      src: r.source_metrics.row_count,
      tgt: r.target_metrics.row_count,
      diff: r.diff_summary,
    });
  }
  if (!rows.size) return <Empty title="No data copied yet">The "Copy the data" step fills this in.</Empty>;
  return (
    <div className="card">
      <table className="list">
        <thead>
          <tr>
            <th>Table</th>
            <th>Copy</th>
            <th>Rows in old system</th>
            <th>Rows in new system</th>
            <th>Numbers match?</th>
          </tr>
        </thead>
        <tbody>
          {Array.from(rows.entries()).map(([key, v]) => (
            <tr key={key}>
              <td>
                <code>{key}</code>
              </td>
              <td>{v.load ? <StatusBadge status={v.load} /> : "—"}</td>
              <td>{v.src != null ? fmt(v.src) : "—"}</td>
              <td>{v.tgt != null ? fmt(v.tgt) : v.rowsCopied != null ? fmt(v.rowsCopied) : "—"}</td>
              <td>
                {v.verdict ? (
                  <span title={v.diff && Object.keys(v.diff).length ? JSON.stringify(v.diff, null, 1) : undefined}>
                    <Badge tone={v.verdict.startsWith("pass") ? "ok" : "bad"} dot>
                      {v.verdict === "pass" ? "Match" : v.verdict === "pass_within_tolerance" ? "Match (within tolerance)" : "Doesn't match"}
                    </Badge>
                  </span>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunsTab({ id }: { id: string }) {
  const { data, error } = useLoad(() => api.runs(id), [id]);
  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!data) return <Spinner />;
  if (!data.length) return <Empty title="Never run">Press Run to start.</Empty>;
  return (
    <div className="card">
      <table className="list">
        <thead>
          <tr>
            <th>Started</th>
            <th>By</th>
            <th>Status</th>
            <th>Took</th>
            <th>Steps</th>
          </tr>
        </thead>
        <tbody>
          {data.map((r) => (
            <tr key={r.id}>
              <td>{timeAgo(r.started_at ?? r.created_at)}</td>
              <td className="muted">{r.started_by}</td>
              <td>
                <StatusBadge status={r.status} />
                {r.error && <div className="faint">{r.error}</div>}
              </td>
              <td>{duration(r.started_at, r.finished_at)}</td>
              <td>
                <span className="row" style={{ gap: 4 }}>
                  {r.stages.map((s) =>
                    s.status === "done" ? (
                      <CheckCircle2 key={s.name} size={15} color="var(--ok)" aria-label={`${s.name} done`} />
                    ) : s.status === "failed" || s.status === "interrupted" ? (
                      <XCircle key={s.name} size={15} color="var(--bad)" aria-label={`${s.name} failed`} />
                    ) : (
                      <Circle key={s.name} size={15} color="var(--text-3)" aria-label={`${s.name} ${s.status}`} />
                    ),
                  )}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SettingsTab({ id }: { id: string }) {
  const { data, error } = useLoad(() => api.project(id), [id]);
  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!data) return <Spinner />;
  const active = data.configs.find((c) => c.is_active) ?? data.configs[0];
  return (
    <div className="card card-pad stack">
      <span className="muted">
        The configuration file this migration runs with (version {active?.version_num ?? "—"}). Passwords are never in it,
        only references to the vault.
      </span>
      <pre className="code" style={{ maxHeight: 560 }}>{active?.raw_yaml ?? "(none)"}</pre>
    </div>
  );
}

/* ---------- page ---------- */
type Tab = "status" | "objects" | "review" | "code" | "data" | "runs" | "settings";

export function MigrationDetail(): React.ReactElement {
  const { id = "" } = useParams();
  const { can } = useApp();
  const mig = useLoad(() => api.migration(id), [id]);
  const [run, setRun] = useState<Run | null>(null);
  const [tab, setTab] = useState<Tab>("status");
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    if (mig.data?.last_run && (!run || mig.data.last_run.id !== run.id)) setRun(mig.data.last_run);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mig.data]);

  const active = run?.status === "running" || run?.status === "queued";
  const poll = useCallback(async () => {
    if (!run) return;
    const r = await api.run(id, run.id);
    setRun(r);
    if (r.status !== "running" && r.status !== "queued") {
      await mig.reload();
      setRefresh((n) => n + 1);
    }
  }, [id, run, mig]);
  usePoll(() => void poll(), 1000, active);

  async function start() {
    setStarting(true);
    setErr(null);
    try {
      setRun(await api.startRun(id));
      setTab("status");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  if (mig.error) return <div className="page"><Notice tone="bad">{mig.error}</Notice></div>;
  const m = mig.data;
  if (!m) return <div className="page"><Spinner /></div>;
  const platform = m.target_platform;

  return (
    <div className="page">
      <PageHead
        crumbs={<Link to="/migrations">Migrations</Link>}
        title={m.name}
        sub={
          <span className="row">
            <ConnectorIcon type={m.source?.type ?? m.source_platform} size="sm" />
            {m.source?.id ? <Link to={`/sources/${m.source.id}`}>{m.source.name}</Link> : m.source_platform}
            <ArrowRight size={14} />
            <ConnectorIcon type={m.destination?.type ?? platform ?? ""} size="sm" />
            {m.destination?.id ? <Link to={`/destinations/${m.destination.id}`}>{m.destination.name}</Link> : platform}
          </span>
        }
        actions={
          can("run_convert") && (
            <button className="btn primary" onClick={() => void start()} disabled={active || starting}>
              {active ? <Spinner size={14} /> : <Play size={15} />} {active ? "Running…" : run ? "Run again" : "Run"}
            </button>
          )
        }
      />
      {err && <Notice tone="bad">{err}</Notice>}
      <div className="tabs" role="tablist">
        {(
          [
            ["status", "Status"],
            ["objects", `Objects (${m.objects})`],
            ["review", "Review"],
            ["code", "Generated code"],
            ["data", "Data & checks"],
            ["runs", "Run history"],
            ["settings", "Settings"],
          ] as [Tab, string][]
        ).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {label}
          </button>
        ))}
      </div>
      <div key={`${tab}-${refresh}`} className="stack">
        {tab === "status" && (
          <>
            <div className="card">
              <div className="card-head">
                <h2>{active ? "Running now" : run ? "Latest run" : "Not run yet"}</h2>
                <div className="spacer" />
                {run && <StatusBadge status={run.status} />}
                {run && <span className="faint">{active ? `started ${timeAgo(run.started_at ?? run.created_at)}` : duration(run.started_at, run.finished_at)}</span>}
              </div>
              <Timeline run={run} platform={platform} />
            </div>
            {run?.status === "failed" && run.error && <Notice tone="bad">{run.error}</Notice>}
            {m.objects > 0 && <Progress m={m} />}
            {!run && (
              <div className="card">
                <Empty title="Ready when you are" action={can("run_convert") && <button className="btn primary" onClick={() => void start()}><Play size={15} /> Run</button>}>
                  A run reads the old system, estimates the work, writes the new code, copies the data and checks the numbers
                  — you can watch each step here.
                </Empty>
              </div>
            )}
            {!active && run && (
              <div className="row faint">
                <Database size={14} /> Open “Data & checks” to compare row counts, or “Review” for anything a person needs to decide.
              </div>
            )}
          </>
        )}
        {tab === "objects" && <ObjectsTab id={id} />}
        {tab === "review" && <ReviewTab id={id} onChange={() => void mig.reload()} />}
        {tab === "code" && <CodeTab id={id} />}
        {tab === "data" && <DataTab id={id} />}
        {tab === "runs" && <RunsTab id={id} />}
        {tab === "settings" && <SettingsTab id={id} />}
      </div>
    </div>
  );
}
