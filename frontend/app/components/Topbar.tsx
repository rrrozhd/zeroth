"use client";

// The console's top bar: a compact operational header with environment state.
// Left: a `{tenant} / {title}` breadcrumb. Right: an environment pill + the
// currently-serving deployment ref.
//
// Every data read happens client-side in an effect and is wrapped in try/catch:
// on any error or unconfigured state we fall back to "—" — the bar never crashes
// and never renders an error boundary.

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { getEnv, getTenant } from "@/app/lib/config";
import { getIdentity, listDeployments } from "@/app/lib/api";
import { TITLE } from "./nav";
import { Pill } from "./primitives";
import { SidebarToggleButton } from "./SidebarToggleButton";

const ENV_TONE: Record<string, string> = {
  local: "muted",
  staging: "warning",
  production: "danger",
};

export function Topbar({
  sidebarCollapsed,
  onSidebarToggle,
}: {
  sidebarCollapsed: boolean;
  onSidebarToggle: () => void;
}) {
  const raw = usePathname() ?? "/";
  const pathname = raw !== "/" && raw.endsWith("/") ? raw.slice(0, -1) : raw;
  const title = TITLE[pathname] ?? "";

  // Read after mount so the prerender and first client render agree.
  const [tenant, setTenant] = useState("default");
  const [workspace, setWorkspace] = useState("scope unavailable");
  const [roles, setRoles] = useState<string[]>([]);
  const [env, setEnv] = useState("local");
  const [served, setServed] = useState("—");

  useEffect(() => {
    setTenant(getTenant());
    setEnv(getEnv());
    let cancelled = false;
    getIdentity()
      .then((identity) => {
        if (cancelled) return;
        setTenant(identity.tenant_id);
        setWorkspace(identity.workspace_id ?? "tenant-wide");
        setRoles(identity.roles ?? []);
      })
      .catch(() => {
        if (!cancelled) {
          setWorkspace("scope unavailable");
          setRoles([]);
        }
      });
    return () => {
      cancelled = true;
    };
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
    <header className="console-topbar">
      <div className="console-topbar-context">
        <SidebarToggleButton
          collapsed={sidebarCollapsed}
          onToggle={onSidebarToggle}
        />
        <div
          className="console-topbar-breadcrumb"
          aria-label={`Scope: ${tenant} / ${workspace}; roles: ${roles.join(", ") || "unavailable"}`}
        >
          <span className="console-topbar-tenant">{tenant}</span>
          <span className="console-topbar-separator">/</span>
          <span className="console-topbar-workspace">{workspace}</span>
          <span className="console-topbar-separator">/</span>
          <span className="console-topbar-title">{title || "Operations"}</span>
        </div>
      </div>

      <div className="console-topbar-state">
        {roles.length > 0 && (
          <span className="console-topbar-role" title={`Active roles: ${roles.join(", ")}`}>
            {roles.join(" · ")}
          </span>
        )}
        <Pill tone={ENV_TONE[env] ?? "muted"}>{env}</Pill>
        <span className="console-topbar-served">
          <span className="console-topbar-served-dot" />
          served: {served}
        </span>
      </div>
    </header>
  );
}
