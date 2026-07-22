"use client";

// Connect bar: a modal editor for the runtime connection settings — API base
// URL, API key, environment label, and tenant. Opened from the sidebar footer.
//
// Security: the API key is persisted to localStorage only (via setConfig). It is
// never logged and never placed in a URL or query string.

import { useState } from "react";
import {
  getApiBase,
  getApiKey,
  getEnv,
  getTenant,
  setConfig,
  setEnvTenant,
} from "@/app/lib/config";
import { Button } from "./primitives";

const ENVS = ["local", "staging", "production"] as const;

const labelStyle: React.CSSProperties = {
  display: "block",
  fontFamily: "var(--font-mono)",
  fontSize: 10.5,
  fontWeight: 500,
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  color: "var(--text-faint)",
  marginBottom: 6,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  background: "var(--bg-card)",
  border: "1px solid var(--hair-strong)",
  borderRadius: 6,
  padding: "8px 10px",
  color: "var(--text-primary)",
  fontFamily: "var(--font-mono)",
  fontSize: 12.5,
  boxSizing: "border-box",
};

export function ConnectBar({ onClose }: { onClose: () => void }) {
  // Seeded from the current config on open. Reading during the static prerender
  // is safe — config.ts guards `window` and returns the defaults.
  const [base, setBase] = useState(() => getApiBase());
  const [key, setKey] = useState(() => getApiKey());
  const [env, setEnv] = useState(() => getEnv());
  const [tenant, setTenant] = useState(() => getTenant());

  function save() {
    setConfig(base, key);
    setEnvTenant(env, tenant);
    // Lazy refresh: reload so every data view re-fetches against the new config.
    location.reload();
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Connection settings"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 80,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.55)",
        padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="z-fade"
        style={{
          width: 420,
          maxWidth: "100%",
          background: "var(--bg-raised-2)",
          border: "1px solid var(--hair-strong)",
          borderRadius: 12,
          padding: 20,
        }}
      >
        <div
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--text-secondary)",
            marginBottom: 16,
          }}
        >
          Connect
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle} htmlFor="cb-base">
            API base URL
          </label>
          <input
            id="cb-base"
            type="text"
            value={base}
            placeholder="same-origin"
            onChange={(e) => setBase(e.target.value)}
            style={inputStyle}
          />
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle} htmlFor="cb-key">
            API key
          </label>
          <input
            id="cb-key"
            type="password"
            value={key}
            autoComplete="off"
            onChange={(e) => setKey(e.target.value)}
            style={inputStyle}
          />
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={labelStyle} htmlFor="cb-env">
            Environment
          </label>
          <select
            id="cb-env"
            value={env}
            onChange={(e) => setEnv(e.target.value)}
            style={inputStyle}
          >
            {ENVS.map((e) => (
              <option key={e} value={e}>
                {e}
              </option>
            ))}
          </select>
        </div>

        <div style={{ marginBottom: 4 }}>
          <label style={labelStyle} htmlFor="cb-tenant">
            Tenant
          </label>
          <input
            id="cb-tenant"
            type="text"
            value={tenant}
            onChange={(e) => setTenant(e.target.value)}
            style={inputStyle}
          />
        </div>

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: 8,
            marginTop: 20,
          }}
        >
          <Button variant="neutral" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={save}>
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}
