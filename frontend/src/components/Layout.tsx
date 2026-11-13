import { ArrowLeftRight, Database, LogOut, Server, Settings, UserCircle2 } from "lucide-react";
import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useApp } from "../context";

export function Layout(): React.ReactElement {
  const { me, clients, client, setClient, signOut } = useApp();
  const navigate = useNavigate();
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">L2C</span>
          <div>
            Accelerator
            <small>Legacy → cloud migrations</small>
          </div>
        </div>
        <div className="client-switch">
          <label className="faint" htmlFor="client-switch">
            Client
          </label>
          <select
            id="client-switch"
            value={client?.slug ?? ""}
            onChange={(e) => {
              if (e.target.value === "__new") navigate("/settings?new-client=1");
              else setClient(e.target.value);
            }}
          >
            {clients.length === 0 && <option value="">No clients yet</option>}
            {clients.map((c) => (
              <option key={c.slug} value={c.slug}>
                {c.name}
              </option>
            ))}
            <option value="__new">+ New client…</option>
          </select>
        </div>
        <nav className="nav">
          <NavLink to="/migrations">
            <ArrowLeftRight size={17} /> Migrations
          </NavLink>
          <NavLink to="/sources">
            <Database size={17} /> Sources
          </NavLink>
          <NavLink to="/destinations">
            <Server size={17} /> Destinations
          </NavLink>
          <div className="nav-label">Workspace</div>
          <NavLink to="/settings">
            <Settings size={17} /> Settings
          </NavLink>
        </nav>
        <div className="sidebar-foot">
          <UserCircle2 size={18} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{me?.username}</div>
            <div className="faint">{me?.role.replace("_", " ")}</div>
          </div>
          <div className="spacer" />
          <button className="btn ghost small" title="Sign out" onClick={signOut}>
            <LogOut size={15} />
          </button>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
