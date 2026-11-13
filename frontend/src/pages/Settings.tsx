import { Plus } from "lucide-react";
import React, { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { Notice, PageHead, Spinner } from "../components/ui";
import { useApp } from "../context";

export function Settings(): React.ReactElement {
  const { me, clients, client, setClient, reloadClients } = useApp();
  const [params, setParams] = useSearchParams();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const focusNew = params.get("new-client") === "1";

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const c = await api.createClient(name.trim());
      await reloadClients();
      setClient(c.slug);
      setName("");
      setParams({});
      setMsg({ tone: "ok", text: `Client "${c.name}" created and selected.` });
    } catch (err) {
      setMsg({ tone: "bad", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page" style={{ maxWidth: 820 }}>
      <PageHead title="Settings" />
      <div className="stack">
        {msg && <Notice tone={msg.tone}>{msg.text}</Notice>}
        <div className="card">
          <div className="card-head">
            <h2>Clients</h2>
          </div>
          <div className="card-pad stack">
            <span className="muted">
              Each client keeps its own sources, destinations, migrations and secrets. Pick the one you're working on in the
              sidebar.
            </span>
            {clients.length > 0 && (
              <table className="list">
                <tbody>
                  {clients.map((c) => (
                    <tr key={c.slug}>
                      <td>
                        <strong>{c.name}</strong> <span className="faint">{c.slug}</span>
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {client?.slug === c.slug ? (
                          <span className="faint">Selected</span>
                        ) : (
                          <button className="btn small" onClick={() => setClient(c.slug)}>
                            Select
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <form className="row" onSubmit={create}>
              <input
                autoFocus={focusNew}
                aria-label="New client name"
                placeholder="New client name, e.g. Acme Retail"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <button className="btn primary" disabled={busy || !name.trim()}>
                {busy ? <Spinner size={14} /> : <Plus size={15} />} Add client
              </button>
            </form>
          </div>
        </div>
        <div className="card">
          <div className="card-head">
            <h2>Your account</h2>
          </div>
          <div className="card-pad stack">
            <div>
              <strong>{me?.username}</strong> <span className="faint">{me?.email}</span>
            </div>
            <div className="muted">Role: {me?.role.replace(/_/g, " ")}</div>
            <div className="checks">
              {me?.permissions.map((p) => (
                <span key={p} className="faint">
                  ✓ {p.replace(/_/g, " ")}
                </span>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
