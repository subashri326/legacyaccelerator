import { ArrowLeft, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import React, { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { ConnectorForm, initialValues, missingRequired } from "../components/ConnectorForm";
import {
  ConnectorIcon,
  Empty,
  Notice,
  PageHead,
  Spinner,
  StatusBadge,
  timeAgo,
  useLoad,
} from "../components/ui";
import { useApp } from "../context";
import type { ConnectorType, SavedConnection, TestResult } from "../types";

export type Kind = "source" | "destination";
const LABEL = { source: "Source", destination: "Destination" } as const;
const API_KIND = { source: "source", destination: "target" } as const;
const BASE = { source: "/sources", destination: "/destinations" } as const;

function NeedClient(): React.ReactElement {
  return (
    <div className="page">
      <div className="card">
        <Empty
          title="Create a client first"
          action={
            <Link className="btn primary" to="/settings?new-client=1">
              <Plus size={15} /> New client
            </Link>
          }
        >
          Sources, destinations and migrations belong to a client.
        </Empty>
      </div>
    </div>
  );
}

/* ---------- list ---------- */
export function ConnectionList({ kind }: { kind: Kind }): React.ReactElement {
  const { client } = useApp();
  const navigate = useNavigate();
  const { data, error, loading } = useLoad(
    () => (client ? api.connections(client.slug, API_KIND[kind]) : Promise.resolve([])),
    [client?.slug, kind],
  );
  if (!client) return <NeedClient />;
  return (
    <div className="page">
      <PageHead
        title={`${LABEL[kind]}s`}
        sub={
          kind === "source"
            ? `The old systems ${client.name} is moving away from.`
            : `The cloud platforms ${client.name} is moving to.`
        }
        actions={
          <Link className="btn primary" to={`${BASE[kind]}/new`}>
            <Plus size={15} /> New {LABEL[kind].toLowerCase()}
          </Link>
        }
      />
      {error && <Notice tone="bad">{error}</Notice>}
      <div className="card">
        {loading ? (
          <div className="empty">
            <Spinner />
          </div>
        ) : !data?.length ? (
          <Empty
            title={`No ${LABEL[kind].toLowerCase()}s yet`}
            action={
              <Link className="btn primary" to={`${BASE[kind]}/new`}>
                <Plus size={15} /> New {LABEL[kind].toLowerCase()}
              </Link>
            }
          >
            {kind === "source"
              ? "Connect an Oracle, SQL Server or Teradata database."
              : "Connect Snowflake, Databricks or Fabric."}
          </Empty>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th>Name</th>
                <th>Connector</th>
                <th>Connection test</th>
                <th>Last tested</th>
              </tr>
            </thead>
            <tbody>
              {data.map((c) => (
                <tr className="click" key={c.id} onClick={() => navigate(`${BASE[kind]}/${c.id}`)}>
                  <td>
                    <div className="row">
                      <ConnectorIcon type={c.type} size="sm" />
                      <strong>{c.name}</strong>
                    </div>
                  </td>
                  <td className="muted">{c.type_name}</td>
                  <td>
                    <StatusBadge status={c.last_test_status} />
                  </td>
                  <td className="muted">{timeAgo(c.last_tested_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/* ---------- picker ---------- */
export function ConnectorPicker({ kind }: { kind: Kind }): React.ReactElement {
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const { data, error } = useLoad(() => api.connectorTypes(), []);
  const types = useMemo(
    () =>
      (data ?? []).filter(
        (t) => (t.kind === kind || t.kind === "both") && t.name.toLowerCase().includes(q.toLowerCase()),
      ),
    [data, kind, q],
  );
  return (
    <div className="page">
      <PageHead
        crumbs={<Link to={BASE[kind]}>{LABEL[kind]}s</Link>}
        title={`New ${LABEL[kind].toLowerCase()}`}
        sub="Choose the system to connect."
      />
      {error && <Notice tone="bad">{error}</Notice>}
      <div className="row" style={{ marginBottom: 16 }}>
        <div style={{ position: "relative", width: "100%", maxWidth: 340 }}>
          <Search size={15} style={{ position: "absolute", left: 10, top: 11, color: "var(--text-3)" }} />
          <input
            className="search"
            style={{ paddingLeft: 32 }}
            placeholder="Search connectors"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>
      <div className="tile-grid">
        {types.map((t) => (
          <button className="tile" key={t.id} onClick={() => navigate(`${BASE[kind]}/new/${t.id}`)}>
            <ConnectorIcon type={t.id} />
            <div>
              <div className="tname">{t.name}</div>
              <div className="tdesc">{t.description}</div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ---------- create / edit ---------- */
function TestOutcome({ result, testing }: { result: TestResult | null; testing: boolean }) {
  if (testing)
    return (
      <Notice tone="info">
        <span className="row">
          <Spinner size={14} /> Testing the connection…
        </span>
      </Notice>
    );
  if (!result) return null;
  return result.ok ? (
    <Notice tone="ok">
      <strong>All connection tests passed.</strong> {result.message} ({result.seconds}s)
    </Notice>
  ) : (
    <Notice tone="bad">
      <strong>The connection test failed.</strong> {result.message}
    </Notice>
  );
}

export function ConnectionCreate({ kind }: { kind: Kind }): React.ReactElement {
  const { typeId = "" } = useParams();
  const { client } = useApp();
  const navigate = useNavigate();
  const types = useLoad(() => api.connectorTypes(), []);
  const type = types.data?.find((t) => t.id === typeId);
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, unknown> | null>(null);
  const [result, setResult] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!client) return <NeedClient />;
  if (!type) return <div className="page">{types.error ? <Notice tone="bad">{types.error}</Notice> : <Spinner />}</div>;
  const v = values ?? initialValues(type);
  const missing = missingRequired(type, v);

  async function test(): Promise<boolean> {
    setTesting(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.testUnsaved(type!.id, v);
      setResult(r);
      return r.ok;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setTesting(false);
    }
  }

  async function setUp() {
    if (!(await test())) return;
    try {
      const c = await api.createConnection({
        client: client!.slug,
        name: name || type!.name,
        type: type!.id,
        kind,
        settings: v,
      });
      await api.testSaved(c.id); // records the passing test on the saved connection
      navigate(`${BASE[kind]}/${c.id}?created=1`);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="page" style={{ maxWidth: 860 }}>
      <PageHead
        crumbs={
          <>
            <Link to={BASE[kind]}>{LABEL[kind]}s</Link> / <Link to={`${BASE[kind]}/new`}>New</Link>
          </>
        }
        icon={<ConnectorIcon type={type.id} size="lg" />}
        title={`Set up ${type.name}`}
        sub={type.description}
      />
      <div className="card card-pad stack">
        <div className="field">
          <label htmlFor="cname">
            {LABEL[kind]} name <span className="req">*</span>
          </label>
          <input
            id="cname"
            placeholder={`e.g. ${client.name} ${type.name}`}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <ConnectorForm type={type} values={v} onChange={setValues} />
        {type.docs && <div className="faint">{type.docs}</div>}
        <div className="faint">Passwords and tokens are stored only in the secure vault and are never shown again.</div>
        <TestOutcome result={result} testing={testing} />
        {error && <Notice tone="bad">{error}</Notice>}
        <div className="row">
          <button className="btn" onClick={() => void test()} disabled={testing || missing.length > 0}>
            Test connection
          </button>
          <div className="spacer" />
          {missing.length > 0 && <span className="faint">Fill in: {missing.join(", ")}</span>}
          <button className="btn primary" onClick={() => void setUp()} disabled={testing || missing.length > 0 || !name}>
            Set up {LABEL[kind].toLowerCase()}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---------- detail ---------- */
export function ConnectionDetail({ kind }: { kind: Kind }): React.ReactElement {
  const { id = "" } = useParams();
  const { client } = useApp();
  const navigate = useNavigate();
  const types = useLoad(() => api.connectorTypes(), []);
  const conn = useLoad(() => (client ? api.connection(client.slug, id) : Promise.reject(new Error("No client"))), [id, client?.slug]);
  const [values, setValues] = useState<Record<string, unknown> | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [result, setResult] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(
    new URLSearchParams(window.location.search).get("created") ? { tone: "ok", text: "Saved. The connection test passed." } : null,
  );
  const [schemas, setSchemas] = useState<string[] | null>(null);

  if (!client) return <NeedClient />;
  const c: SavedConnection | null = conn.data;
  const type: ConnectorType | undefined = types.data?.find((t) => t.id === c?.type);
  if (conn.error) return <div className="page"><Notice tone="bad">{conn.error}</Notice></div>;
  if (!c || !type) return <div className="page"><Spinner /></div>;
  const v = values ?? initialValues(type, c.settings);

  async function retest() {
    setTesting(true);
    setResult(null);
    try {
      setResult(await api.testSaved(c!.id));
      await conn.reload();
    } catch (e) {
      setMsg({ tone: "bad", text: (e as Error).message });
    } finally {
      setTesting(false);
    }
  }
  async function save() {
    try {
      setTesting(true);
      const r = await api.testUnsaved(type!.id, v, c!.id);
      setResult(r);
      if (!r.ok) return;
      await api.updateConnection(c!.id, { name: name ?? c!.name, settings: v });
      await api.testSaved(c!.id);
      setValues(null);
      setMsg({ tone: "ok", text: "Changes saved and tested." });
      await conn.reload();
    } catch (e) {
      setMsg({ tone: "bad", text: (e as Error).message });
    } finally {
      setTesting(false);
    }
  }
  async function remove() {
    if (!window.confirm(`Delete "${c!.name}"? This can't be undone.`)) return;
    try {
      await api.deleteConnection(c!.id);
      navigate(BASE[kind]);
    } catch (e) {
      setMsg({ tone: "bad", text: (e as Error).message });
    }
  }
  async function browse() {
    try {
      setSchemas((await api.schemas(c!.id)).schemas);
    } catch (e) {
      setMsg({ tone: "bad", text: (e as Error).message });
    }
  }

  return (
    <div className="page" style={{ maxWidth: 900 }}>
      <PageHead
        crumbs={<Link to={BASE[kind]}><ArrowLeft size={12} /> {LABEL[kind]}s</Link>}
        icon={<ConnectorIcon type={c.type} size="lg" />}
        title={c.name}
        sub={
          <span className="row">
            {c.type_name} · <StatusBadge status={c.last_test_status} />
            <span className="faint">tested {timeAgo(c.last_tested_at)}</span>
          </span>
        }
        actions={
          <button className="btn" onClick={() => void retest()} disabled={testing}>
            {testing ? <Spinner size={14} /> : <RefreshCw size={14} />} Retest
          </button>
        }
      />
      <div className="stack">
        {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
        <TestOutcome result={result} testing={testing} />
        {c.last_test_status === "failed" && !result && c.last_test_message && (
          <Notice tone="bad">Last test failed: {c.last_test_message}</Notice>
        )}
        <div className="card">
          <div className="card-head"><h2>Settings</h2></div>
          <div className="card-pad stack">
            <div className="field">
              <label htmlFor="cname">Name</label>
              <input id="cname" value={name ?? c.name} onChange={(e) => setName(e.target.value)} />
            </div>
            <ConnectorForm type={type} values={v} onChange={setValues} savedSecrets={c.secret_fields_set} />
            <div className="row">
              <button className="btn danger" onClick={() => void remove()}>
                <Trash2 size={14} /> Delete
              </button>
              <div className="spacer" />
              <button
                className="btn primary"
                disabled={testing || missingRequired(type, v, c.secret_fields_set).length > 0 || (values === null && name === null)}
                onClick={() => void save()}
              >
                Save changes
              </button>
            </div>
          </div>
        </div>
        {kind === "source" && (
          <div className="card">
            <div className="card-head">
              <h2>What this connection can see</h2>
              <div className="spacer" />
              <button className="btn small" onClick={() => void browse()}>Browse schemas</button>
            </div>
            {schemas && (
              <div className="card-pad">
                {schemas.length ? (
                  <div className="checks">{schemas.map((s) => <span key={s}><code>{s}</code></span>)}</div>
                ) : (
                  <span className="muted">No schemas visible to this login.</span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
