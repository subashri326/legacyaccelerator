import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api/client";
import type { Client, Me } from "./types";

interface AppState {
  me: Me | null;
  clients: Client[];
  client: Client | null;
  setClient: (slug: string) => void;
  reloadClients: () => Promise<void>;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => void;
  can: (permission: string) => boolean;
}

const Ctx = createContext<AppState | null>(null);
const CLIENT_KEY = "l2c_client";

export function AppProvider({ children }: { children: React.ReactNode }): React.ReactElement {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const [clients, setClients] = useState<Client[]>([]);
  const [slug, setSlug] = useState<string | null>(() => {
    try {
      return localStorage.getItem(CLIENT_KEY);
    } catch {
      return null;
    }
  });

  const reloadClients = useCallback(async () => {
    const list = await api.clients();
    setClients(list);
  }, []);

  const loadSession = useCallback(async () => {
    try {
      setMe(await api.me());
      await reloadClients();
    } catch {
      setMe(null);
    } finally {
      setReady(true);
    }
  }, [reloadClients]);

  useEffect(() => {
    api.onUnauthorized = () => setMe(null);
    if (api.signedIn) void loadSession();
    else setReady(true);
  }, [loadSession]);

  const setClient = (s: string) => {
    setSlug(s);
    try {
      localStorage.setItem(CLIENT_KEY, s);
    } catch {
      /* ignore */
    }
  };

  const client = clients.find((c) => c.slug === slug) ?? clients[0] ?? null;

  const value: AppState = {
    me,
    clients,
    client,
    setClient,
    reloadClients,
    signIn: async (u, p) => {
      const tok = await api.login(u, p);
      api.setToken(tok.access_token);
      await loadSession();
    },
    signOut: () => {
      api.setToken(null);
      setMe(null);
    },
    can: (perm) => !!me?.permissions.includes(perm),
  };
  if (!ready) return <div className="login muted">Loading…</div>;
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside AppProvider");
  return v;
}
