"use client";

// The console's top bar. 52px, dark chrome, bottom hairline.
// Left: a `{tenant} / {title}` breadcrumb. Right: an environment pill + the
// currently-serving deployment ref.
//
// Every data read happens client-side in an effect and is wrapped in try/catch:
// on any error or unconfigured state we fall back to "—" — the bar never crashes
// and never renders an error boundary.

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { getEnv, getTenant } from "@/app/lib/config";
import { listDeployments } from "@/app/lib/api";
import { TITLE } from "./nav";
import { Pill } from "./primitives";

const ENV_TONE: Record<string, string> = {
  local: "muted",
  staging: "warning",
  production: "danger",
};

export function Topbar() {
  const raw = usePathname() ?? "/";
  const pathname = raw !== "/" && raw.endsWith("/") ? raw.slice(0, -1) : raw;
  const title = TITLE[pathname] ?? "";

  // Read after mount so the prerender and first client render agree.
  const [tenant, setTenant] = useState("default");
  const [env, setEnv] = useState("local");
  const [served, setServed] = useState("—");

  useEffect(() => {
    setTenant(getTenant());
    setEnv(getEnv());
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const deployments = await listDeployments();
        const serving = deployments.find((d) => d.serving);
        if (!cancelled) setServed(serving ? serving.deployment_ref : "—");
      } catch {
        if (!cancelled) setServed("—");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <header
      style={{
        height: 52,
        minHeight: 52,
        flexShrink: 0,
        background: "var(--bg-chrome)",
        borderBottom: "1px solid var(--hair)",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0 20px",
      }}
    >
      <div style={{ fontFamily: "var(--font-mono)", fontSize: 13 }}>
        <span style={{ color: "var(--text-faint)" }}>{tenant}</span>
        <span style={{ color: "var(--text-faint)", margin: "0 6px" }}>/</span>
        <span style={{ color: "var(--text-primary)" }}>{title}</span>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <Pill tone={ENV_TONE[env] ?? "muted"}>{env}</Pill>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            background: "var(--bg-card)",
            border: "1px solid var(--hair)",
            borderRadius: 5,
            padding: "3px 8px",
            color: "var(--text-muted)",
          }}
        >
          served: {served}
        </span>
      </div>
    </header>
  );
}
