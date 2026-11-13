import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { AppProvider, useApp } from "./context";
import { ConnectionCreate, ConnectionDetail, ConnectionList, ConnectorPicker, type Kind } from "./pages/Connections";
import { Login } from "./pages/Login";
import { MigrationDetail } from "./pages/MigrationDetail";
import { MigrationList, MigrationWizard } from "./pages/Migrations";
import { Settings } from "./pages/Settings";
import "./styles.css";

function connectionRoutes(kind: Kind, base: string) {
  return (
    <>
      <Route path={base} element={<ConnectionList kind={kind} />} />
      <Route path={`${base}/new`} element={<ConnectorPicker kind={kind} />} />
      <Route path={`${base}/new/:typeId`} element={<ConnectionCreate kind={kind} />} />
      <Route path={`${base}/:id`} element={<ConnectionDetail kind={kind} />} />
    </>
  );
}

function Routed(): React.ReactElement {
  const { me } = useApp();
  if (!me) return <Login />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Navigate to="/migrations" replace />} />
        <Route path="/migrations" element={<MigrationList />} />
        <Route path="/migrations/new" element={<MigrationWizard />} />
        <Route path="/migrations/:id" element={<MigrationDetail />} />
        {connectionRoutes("source", "/sources")}
        {connectionRoutes("destination", "/destinations")}
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/migrations" replace />} />
      </Route>
    </Routes>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppProvider>
        <Routed />
      </AppProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
