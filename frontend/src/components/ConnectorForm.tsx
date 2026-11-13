import React from "react";
import type { ConnectorField, ConnectorType } from "../types";

export function initialValues(
  type: ConnectorType,
  saved?: Record<string, unknown>,
): Record<string, unknown> {
  const v: Record<string, unknown> = {};
  for (const f of type.fields) {
    if (f.secret) v[f.name] = "";
    else if (saved && saved[f.name] !== undefined) v[f.name] = saved[f.name];
    else v[f.name] = f.default ?? (f.type === "boolean" ? false : "");
  }
  return v;
}

export function missingRequired(
  type: ConnectorType,
  values: Record<string, unknown>,
  savedSecrets: string[] = [],
): string[] {
  return type.fields
    .filter(
      (f) =>
        f.required &&
        (values[f.name] === "" || values[f.name] == null) &&
        !(f.secret && savedSecrets.includes(f.name)),
    )
    .map((f) => f.label);
}

function FieldInput({
  f,
  value,
  onChange,
  secretSaved,
}: {
  f: ConnectorField;
  value: unknown;
  onChange: (v: unknown) => void;
  secretSaved: boolean;
}): React.ReactElement {
  const id = `f-${f.name}`;
  if (f.type === "boolean") {
    return (
      <label className="row" htmlFor={id} style={{ fontWeight: 600, fontSize: 13 }}>
        <input id={id} type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
        {f.label}
      </label>
    );
  }
  if (f.type === "select") {
    return (
      <select id={id} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {f.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      id={id}
      type={f.type === "password" ? "password" : f.type === "integer" ? "number" : "text"}
      value={String(value ?? "")}
      autoComplete={f.secret ? "new-password" : "off"}
      placeholder={secretSaved ? "Saved – leave blank to keep" : f.help && f.help.startsWith("e.g.") ? f.help.slice(5) : ""}
      onChange={(e) => onChange(f.type === "integer" && e.target.value !== "" ? Number(e.target.value) : e.target.value)}
    />
  );
}

export function ConnectorForm({
  type,
  values,
  onChange,
  savedSecrets = [],
}: {
  type: ConnectorType;
  values: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  savedSecrets?: string[];
}): React.ReactElement {
  return (
    <div className="form-grid">
      {type.fields.map((f) => (
        <div className={`field ${f.type === "boolean" || f.help.length > 50 ? "wide" : ""}`} key={f.name}>
          {f.type !== "boolean" && (
            <label htmlFor={`f-${f.name}`}>
              {f.label} {f.required && !(f.secret && savedSecrets.includes(f.name)) && <span className="req">*</span>}
            </label>
          )}
          <FieldInput
            f={f}
            value={values[f.name]}
            secretSaved={f.secret && savedSecrets.includes(f.name)}
            onChange={(v) => onChange({ ...values, [f.name]: v })}
          />
          {f.help && !f.help.startsWith("e.g.") && <span className="help">{f.help}</span>}
        </div>
      ))}
    </div>
  );
}
