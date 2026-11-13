import { AlertTriangle, CheckCircle2, Info, Loader2, XCircle } from "lucide-react";
import React, { useCallback, useEffect, useRef, useState } from "react";

/* Connector "logos": generic colored initials (no vendor trademarks). */
const ICON_STYLE: Record<string, { bg: string; label: string }> = {
  oracle: { bg: "#c74634", label: "Or" },
  sqlserver: { bg: "#a4373a", label: "MS" },
  teradata: { bg: "#e5541b", label: "Td" },
  postgres: { bg: "#336791", label: "Pg" },
  snowflake: { bg: "#29b5e8", label: "Sf" },
  databricks: { bg: "#ef3e2d", label: "Db" },
  fabric: { bg: "#117865", label: "Fb" },
  duckdb: { bg: "#8a7a1f", label: "Dk" },
};

export function ConnectorIcon({
  type,
  size = "md",
}: {
  type: string;
  size?: "sm" | "md" | "lg";
}): React.ReactElement {
  const s = ICON_STYLE[type] ?? { bg: "#6b6a88", label: type.slice(0, 2) };
  return (
    <span className={`cicon ${size === "md" ? "" : size}`} style={{ background: s.bg }} aria-hidden>
      {s.label}
    </span>
  );
}

type Tone = "ok" | "bad" | "warn" | "info" | "accent" | "neutral";

export function Badge({
  tone = "neutral",
  children,
  dot = false,
}: {
  tone?: Tone;
  children: React.ReactNode;
  dot?: boolean;
}): React.ReactElement {
  return (
    <span className={`badge ${tone === "neutral" ? "" : tone}`}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

const RUN_TONE: Record<string, Tone> = {
  succeeded: "ok",
  passed: "ok",
  pass: "ok",
  completed: "ok",
  done: "ok",
  failed: "bad",
  fail: "bad",
  interrupted: "warn",
  running: "info",
  queued: "info",
};
const RUN_LABEL: Record<string, string> = {
  succeeded: "Succeeded",
  failed: "Failed",
  interrupted: "Interrupted",
  running: "Running",
  queued: "Starting",
  passed: "Connected",
};

export function StatusBadge({ status }: { status: string | null | undefined }): React.ReactElement {
  if (!status) return <Badge>Not tested</Badge>;
  return (
    <Badge tone={RUN_TONE[status] ?? "neutral"} dot>
      {RUN_LABEL[status] ?? status[0].toUpperCase() + status.slice(1)}
    </Badge>
  );
}

export function Notice({
  tone,
  children,
}: {
  tone: "ok" | "bad" | "info" | "warn";
  children: React.ReactNode;
}): React.ReactElement {
  const Icon = { ok: CheckCircle2, bad: XCircle, info: Info, warn: AlertTriangle }[tone];
  return (
    <div className={`notice ${tone}`} role={tone === "bad" ? "alert" : "status"}>
      <Icon size={18} style={{ flex: "none", marginTop: 1 }} />
      <div>{children}</div>
    </div>
  );
}

export function Spinner({ size = 16 }: { size?: number }): React.ReactElement {
  return <Loader2 size={size} className="spin" />;
}

export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="empty">
      <h2>{title}</h2>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function PageHead({
  title,
  sub,
  crumbs,
  icon,
  actions,
}: {
  title: React.ReactNode;
  sub?: React.ReactNode;
  crumbs?: React.ReactNode;
  icon?: React.ReactNode;
  actions?: React.ReactNode;
}): React.ReactElement {
  return (
    <>
      {crumbs && <div className="crumbs">{crumbs}</div>}
      <div className="page-head">
        {icon}
        <div>
          <h1>{title}</h1>
          {sub && <div className="sub">{sub}</div>}
        </div>
        <div className="spacer" />
        {actions}
      </div>
    </>
  );
}

/** Load data once (and on demand); shows errors instead of throwing. */
export function useLoad<T>(
  load: () => Promise<T>,
  deps: React.DependencyList,
): { data: T | null; error: string | null; loading: boolean; reload: () => Promise<void> } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;
  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload };
}

/** Call `fn` every `ms` while `active`. */
export function usePoll(fn: () => void, ms: number, active: boolean): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => ref.current(), ms);
    return () => window.clearInterval(t);
  }, [ms, active]);
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const t = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`).getTime();
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function duration(start?: string | null, end?: string | null): string {
  if (!start) return "";
  const fix = (x: string) => (x.endsWith("Z") || x.includes("+") ? x : `${x}Z`);
  const ms = (end ? new Date(fix(end)).getTime() : Date.now()) - new Date(fix(start)).getTime();
  const s = Math.max(0, Math.round(ms / 100) / 10);
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function fmt(n: number | null | undefined): string {
  return n == null ? "–" : n.toLocaleString();
}
