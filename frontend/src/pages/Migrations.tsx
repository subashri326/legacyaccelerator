import { ArrowRight, Check, FileUp, Plus, X } from "lucide-react";
import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import {
  ConnectorIcon,
  Empty,
  Notice,
  PageHead,
  Spinner,
  StatusBadge,
  fmt,
  timeAgo,
  useLoad,
} from "../components/ui";
import { useApp } from "../context";
import type { SavedConnection, Upload } from "../types";

/* ---------- list ---------- */
export function MigrationList(): React.ReactElement {
  const { client } = useApp();
  const navigate = useNavigate();
  const { data, error, loading } = useLoad(() => api.migrations(), []);
  const rows = (data ?? []).filter((m) => !client || m.client === client.slug);
  return (
    <div className="page">
      <PageHead
        title="Migrations"
        sub={client ? `Every move from an old system to the cloud for ${client.name}.` : undefined}
        actions={
          client && (
            <Link className="btn primary" to="/migrations/new">
              <Plus size={15} /> New migration
            </Link>
          )
        }
      />
      {error && <Notice tone="bad">{error}</Notice>}
      <div className="card">
        {loading ? (
          <div className="empty">
            <Spinner />
          </div>
        ) : !rows.length ? (
          <Empty
            title="No migrations yet"
            action={
              client ? (
                <Link className="btn primary" to="/migrations/new">
                  <Plus size={15} /> New migration
                </Link>
              ) : (
                <Link className="btn primary" to="/settings?new-client=1">
                  <Plus size={15} /> New client
                </Link>
              )
            }
          >
            A migration links one source to one destination and moves tables, code and jobs across.
          </Empty>
        ) : (
          <table className="list">
            <thead>
              <tr>
                <th>Name</th>
                <th>From → to</th>
                <th>Objects</th>
                <th>Validated</th>
                <th>Last run</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <tr className="click" key={m.id} onClick={() => navigate(`/migrations/${m.id}`)}>
                  <td>
                    <strong>{m.name}</strong>
                  </td>
                  <td>
                    <div className="row">
                      <ConnectorIcon type={m.source?.type ?? m.source_platform} size="sm" />
                      <ArrowRight size={14} className="muted" />
                      <ConnectorIcon type={m.destination?.type ?? m.target_platform ?? ""} size="sm" />
                      <span className="muted">
                        {m.source?.name ?? m.source_platform} → {m.destination?.name ?? m.target_platform}
                      </span>
                    </div>
                  </td>
                  <td>{fmt(m.objects)}</td>
                  <td>
                    {fmt(m.by_state.Validated ?? 0)} / {fmt(m.objects)}
                  </td>
                  <td>
                    {m.last_run ? (
                      <span className="row">
                        <StatusBadge status={m.last_run.status} />
                        <span className="faint">{timeAgo(m.last_run.started_at ?? m.last_run.created_at)}</span>
                      </span>
                    ) : (
                      <span className="faint">Never run</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

/* ---------- wizard ---------- */
const STEPS = ["Source", "Destination", "What to move", "Options"];

function ConnectionChoice({
  list,
  value,
  onChange,
  kind,
}: {
  list: SavedConnection[];
  value: string;
  onChange: (id: string) => void;
  kind: "source" | "destination";
}) {
  if (!list.length)
    return (
      <Empty
        title={`No ${kind}s yet`}
        action={
          <Link className="btn primary" to={kind === "source" ? "/sources/new" : "/destinations/new"}>
            <Plus size={15} /> New {kind}
          </Link>
        }
      >
        Set one up first, then come back here.
      </Empty>
    );
  return (
    <div className="stack">
      {list.map((c) => (
        <div
          key={c.id}
          role="radio"
          aria-checked={value === c.id}
          tabIndex={0}
          className={`choice ${value === c.id ? "on" : ""}`}
          onClick={() => onChange(c.id)}
          onKeyDown={(e) => e.key === "Enter" && onChange(c.id)}
        >
          <ConnectorIcon type={c.type} />
          <div>
            <strong>{c.name}</strong>
            <div className="faint">{c.type_name}</div>
          </div>
          <div className="spacer" />
          <StatusBadge status={c.last_test_status} />
        </div>
      ))}
      <Link className="faint" to={kind === "source" ? "/sources/new" : "/destinations/new"}>
        + Set up another {kind}
      </Link>
    </div>
  );
}

function Uploader({
  client,
  label,
  accept,
  multiple,
  files,
  setFiles,
}: {
  client: string;
  label: string;
  accept: string;
  multiple: boolean;
  files: Upload[];
  setFiles: (f: Upload[]) => void;
}) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = `up-${label.replace(/\W/g, "")}`;
  async function add(list: FileList | null) {
    if (!list?.length) return;
    setBusy(true);
    setError(null);
    try {
      const done: Upload[] = [];
      for (const f of Array.from(list)) done.push(await api.upload(client, f));
      setFiles(multiple ? [...files, ...done] : done.slice(-1));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="stack">
      <label
        htmlFor={id}
        className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          void add(e.dataTransfer.files);
        }}
      >
        {busy ? <Spinner /> : <FileUp size={22} />}
        <div style={{ marginTop: 6 }}>{label}</div>
        <div className="faint">Drop files here or click to choose</div>
      </label>
      <input
        id={id}
        type="file"
        hidden
        accept={accept}
        multiple={multiple}
        onChange={(e) => void add(e.target.files)}
      />
      {error && <Notice tone="bad">{error}</Notice>}
      {files.map((f) => (
        <div className="row" key={f.path}>
          <code>{f.name}</code>
          <span className="faint">
            {f.tool ? `${f.tool} · ` : ""}
            {fmt(Math.round(f.size / 1024))} KB
          </span>
          <div className="spacer" />
          <button className="btn ghost small" onClick={() => setFiles(files.filter((x) => x.path !== f.path))}>
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}

export function MigrationWizard(): React.ReactElement {
  const { client } = useApp();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const sources = useLoad(() => (client ? api.connections(client.slug, "source") : Promise.resolve([])), [client?.slug]);
  const dests = useLoad(() => (client ? api.connections(client.slug, "target") : Promise.resolve([])), [client?.slug]);
  const [sourceId, setSourceId] = useState("");
  const [destId, setDestId] = useState("");
  const [schemaList, setSchemaList] = useState<string[] | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [schemas, setSchemas] = useState<string[]>([]);
  const [readFrom, setReadFrom] = useState<"database" | "file">("database");
  const [catalog, setCatalog] = useState<Upload[]>([]);
  const [jobs, setJobs] = useState<Upload[]>([]);
  const [name, setName] = useState("");
  const [layer, setLayer] = useState("curated");
  const [schemaTemplate, setSchemaTemplate] = useState("{layer}_{source_schema}");
  const [tableCase, setTableCase] = useState("upper");
  const [columnCase, setColumnCase] = useState("lower");
  const [level, setLevel] = useState("L2");
  const [tolerance, setTolerance] = useState(0);
  const [startNow, setStartNow] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!client)
    return (
      <div className="page">
        <Notice tone="warn">Create a client first in Settings.</Notice>
      </div>
    );

  const source = sources.data?.find((c) => c.id === sourceId);
  const dest = dests.data?.find((c) => c.id === destId);

  async function loadSchemas() {
    setSchemaList(null);
    setSchemaError(null);
    try {
      setSchemaList((await api.schemas(sourceId)).schemas);
    } catch (e) {
      setSchemaError((e as Error).message);
    }
  }

  const canNext = [
    !!sourceId,
    !!destId,
    readFrom === "database" ? schemas.length > 0 : catalog.length > 0,
    !!name.trim(),
  ][step];

  function next() {
    if (step === 1 && readFrom === "database" && schemaList === null) void loadSchemas();
    if (step === 2 && !name) setName(`${source?.name ?? "Source"} to ${dest?.name ?? "destination"}`);
    setStep(step + 1);
  }

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const m = await api.createMigration({
        client: client!.slug,
        name: name.trim(),
        source_connection_id: sourceId,
        destination_connection_id: destId,
        schemas: readFrom === "database" ? schemas : [],
        catalog_file: readFrom === "file" ? catalog[0]?.path : null,
        etl_files: jobs.map((j) => j.path),
        naming: { schema_template: schemaTemplate, table_case: tableCase, column_case: columnCase, layer },
        reconciliation_level: level,
        numeric_tolerance: tolerance,
      });
      if (startNow) await api.startRun(m.id);
      navigate(`/migrations/${m.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="page" style={{ maxWidth: 860 }}>
      <PageHead crumbs={<Link to="/migrations">Migrations</Link>} title="New migration" />
      <div className="steps">
        {STEPS.map((s, i) => (
          <React.Fragment key={s}>
            {i > 0 && <span className="sep">—</span>}
            <span className={`s ${i === step ? "on" : i < step ? "done" : ""}`}>
              <span className="n">{i < step ? <Check size={13} /> : i + 1}</span>
              {s}
            </span>
          </React.Fragment>
        ))}
      </div>
      <div className="card card-pad stack">
        {step === 0 && (
          <>
            <h2>Which old system are you moving from?</h2>
            {sources.loading ? <Spinner /> : <ConnectionChoice kind="source" list={sources.data ?? []} value={sourceId} onChange={setSourceId} />}
          </>
        )}
        {step === 1 && (
          <>
            <h2>Where should it go?</h2>
            {dests.loading ? <Spinner /> : <ConnectionChoice kind="destination" list={dests.data ?? []} value={destId} onChange={setDestId} />}
          </>
        )}
        {step === 2 && (
          <>
            <h2>What should be moved?</h2>
            <div className="row wrap">
              <div className={`choice ${readFrom === "database" ? "on" : ""}`} onClick={() => setReadFrom("database")}>
                <input type="radio" readOnly checked={readFrom === "database"} />
                <div>
                  <strong>Read the tables from {source?.name}</strong>
                  <div className="faint">Pick schemas; the accelerator lists the tables itself.</div>
                </div>
              </div>
              <div className={`choice ${readFrom === "file" ? "on" : ""}`} onClick={() => setReadFrom("file")}>
                <input type="radio" readOnly checked={readFrom === "file"} />
                <div>
                  <strong>Use a DDL export file</strong>
                  <div className="faint">For when the DBA hands you a script instead of access.</div>
                </div>
              </div>
            </div>
            {readFrom === "database" ? (
              <div className="stack">
                <div className="row">
                  <strong>Schemas</strong>
                  <div className="spacer" />
                  <button className="btn small" onClick={() => void loadSchemas()}>Refresh</button>
                </div>
                {schemaError && <Notice tone="bad">{schemaError}</Notice>}
                {schemaList === null && !schemaError ? (
                  <Spinner />
                ) : schemaList && schemaList.length === 0 ? (
                  <span className="muted">This login can't see any schemas.</span>
                ) : (
                  <div className="checks">
                    {(schemaList ?? []).map((s) => (
                      <label key={s}>
                        <input
                          type="checkbox"
                          checked={schemas.includes(s)}
                          onChange={(e) => setSchemas(e.target.checked ? [...schemas, s] : schemas.filter((x) => x !== s))}
                        />
                        <code>{s}</code>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <Uploader client={client.slug} label="DDL export (.sql / .ddl)" accept=".sql,.ddl,.txt" multiple={false} files={catalog} setFiles={setCatalog} />
            )}
            <div className="stack">
              <strong>ETL jobs and stored code (optional)</strong>
              <span className="faint">SSIS packages (.dtsx), Informatica exports (.xml), DataStage (.dsx), BTEQ or SQL scripts.</span>
              <Uploader client={client.slug} label="Job exports" accept=".dtsx,.xml,.dsx,.sql,.btq,.bteq,.zip" multiple files={jobs} setFiles={setJobs} />
            </div>
          </>
        )}
        {step === 3 && (
          <>
            <h2>Final options</h2>
            <div className="field">
              <label htmlFor="mname">Migration name <span className="req">*</span></label>
              <input id="mname" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="tmpl">Target schema name</label>
                <input id="tmpl" value={schemaTemplate} onChange={(e) => setSchemaTemplate(e.target.value)} />
                <span className="help">
                  {"{layer}"} and {"{source_schema}"} are filled in for you.
                </span>
              </div>
              <div className="field">
                <label htmlFor="layer">Layer</label>
                <input id="layer" value={layer} onChange={(e) => setLayer(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="tc">Table names</label>
                <select id="tc" value={tableCase} onChange={(e) => setTableCase(e.target.value)}>
                  <option value="upper">UPPER CASE</option>
                  <option value="lower">lower case</option>
                  <option value="preserve">Keep as they are</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="cc">Column names</label>
                <select id="cc" value={columnCase} onChange={(e) => setColumnCase(e.target.value)}>
                  <option value="lower">lower case</option>
                  <option value="upper">UPPER CASE</option>
                  <option value="preserve">Keep as they are</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="lvl">How carefully to check the numbers</label>
                <select id="lvl" value={level} onChange={(e) => setLevel(e.target.value)}>
                  <option value="L1">Row counts only</option>
                  <option value="L2">Row counts, totals and nulls</option>
                  <option value="L3">Plus per-column fingerprints</option>
                  <option value="L4">Plus row-by-row comparison</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="tol">Allowed difference in totals</label>
                <input id="tol" type="number" min={0} step="0.0001" value={tolerance} onChange={(e) => setTolerance(Number(e.target.value))} />
                <span className="help">0 means totals must match exactly.</span>
              </div>
            </div>
            <label className="row">
              <input type="checkbox" checked={startNow} onChange={(e) => setStartNow(e.target.checked)} />
              Start the migration as soon as it's created
            </label>
          </>
        )}
        {error && <Notice tone="bad">{error}</Notice>}
        <div className="row">
          {step > 0 && (
            <button className="btn" onClick={() => setStep(step - 1)} disabled={busy}>
              Back
            </button>
          )}
          <div className="spacer" />
          {step < STEPS.length - 1 ? (
            <button className="btn primary" disabled={!canNext} onClick={next}>
              Next
            </button>
          ) : (
            <button className="btn primary" disabled={!canNext || busy} onClick={() => void create()}>
              {busy && <Spinner size={14} />} {startNow ? "Create and start" : "Create migration"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
