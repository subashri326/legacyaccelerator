import React, { useState } from "react";
import { Notice, Spinner } from "../components/ui";
import { useApp } from "../context";

export function Login(): React.ReactElement {
  const { signIn } = useApp();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username, password);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="card card-pad stack" onSubmit={submit}>
        <div className="brand" style={{ padding: 0 }}>
          <span className="brand-mark">L2C</span>
          <div>
            Data Modernization Accelerator
            <small>Sign in to continue</small>
          </div>
        </div>
        {error && <Notice tone="bad">{error}</Notice>}
        <div className="field">
          <label htmlFor="u">Username</label>
          <input id="u" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="p">Password</label>
          <input
            id="p"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <button className="btn primary" disabled={busy || !username || !password}>
          {busy && <Spinner size={14} />} Sign in
        </button>
      </form>
    </div>
  );
}
