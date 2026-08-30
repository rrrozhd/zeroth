"use client";

// The Overview screen — the operator's landing page, wired to the live API.
//
// Every read happens client-side in an effect (see `useLoad`) and degrades
// gracefully: an unconfigured or unreachable API surfaces as an inline error
// (with Retry) or an empty state, never a crash or a thrown boundary. The API
// key is exchanged for an HttpOnly session cookie — it is never logged and
// never placed in a URL.

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Button, Card, Pill, Skeleton, StatusDot } from "@/app/components/primitives";
import { useAuditVerification } from "@/app/components/auditVerificationContext";
import { useToast } from "@/app/components/Toast";
import { useRegulus } from "@/app/components/regulusContext";
import { RUN_TONE } from "@/app/components/runTone";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  errMsg,
  getHealth,
  listDeployments,
  listRuns,
  rollbackDeployment,
  type AdminRunList,
  type DeploymentSummary,
  type HealthResponse,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

export default function Overview() {
  const router = useRouter();
  const toast = useToast();
  const reg = useRegulus();

  const health = useLoad<HealthResponse>(getHealth);
  const dep = useLoad<DeploymentSummary[]>(listDeployments);
  const runs = useLoad<AdminRunList>(listRuns);
  const { verifiedAt } = useAuditVerification();

  // Connection config is read after mount so the static prerender and the
  // first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);
  const connected = mounted && isConfigured();

  const [rollingBack, setRollingBack] = useState<string | null>(null);
  const doRollback = async (d: DeploymentSummary) => {
    if (!window.confirm(`Roll back ${d.deployment_ref} to ${d.graph_version_ref}?`)) return;
    // The endpoint pins a new version to an earlier graph version; derive the
    // target from the row's ref (`{graph_id}@{version}`).
    const target = Number(d.graph_version_ref.split("@").pop());
    if (!Number.isFinite(target)) {
      toast(`Can't derive a graph version from ${d.graph_version_ref}`);
      return;
    }
    const key = `${d.deployment_ref}@${d.version}`;
    setRollingBack(key);
    try {
      await rollbackDeployment(d.deployment_ref, target);
      toast(`Registered rollback of ${d.deployment_ref} to graph v${target}`);
      dep.reload();
    } catch (e) {
      toast(`Rollback failed: ${errMsg(e)}`);
    } finally {
      setRollingBack(null);
    }
  };

  // --- Health tile derivations ---
  const apiTile = (() => {
    if (health.error || (!health.loading && !health.data)) {
      return { tone: "danger", value: "unreachable", sub: "check Connect" };
    }
    if (!health.data) return { tone: "muted", value: "—", sub: "checking…" };
    const ok = health.data.status === "ok";
    return {
      tone: ok ? "success" : "warning",
      value: ok ? "operational" : health.data.status,
      sub: health.data.deployment_ref,
    };
  })();

  const econTile =
    reg === "enabled"
      ? { tone: "success", value: "mounted", sub: "/regulus" }
      : reg === "absent"
        ? { tone: "muted", value: "not enabled", sub: "econ plane off" }
        : { tone: "muted", value: "detecting…", sub: "/regulus" };

  const checklist = [
    { label: "Connect to the API", done: connected },
    { label: "Create a deployment", done: (dep.data?.length ?? 0) >= 1 },
    { label: "Submit a run", done: (runs.data?.runs.length ?? 0) >= 1 },
    { label: "Verify the audit chain", done: verifiedAt !== null },
  ];

  return (
    <div className="overview-page" style={{ maxWidth: 980, margin: "0 auto", padding: "28px 28px 48px" }}>
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 19, fontWeight: 600, letterSpacing: "-0.01em" }}>Overview</h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          Operate and author your Zeroth multi-agent apps.
        </p>
      </header>

      {/* One operational strip: related status belongs in one surface. */}
      <Card pad={0} style={{ overflow: "hidden", marginBottom: 10 }}>
        <div className="overview-health-grid">
          <HealthTile
            label="API service"
            {...apiTile}
            loading={health.loading && !health.data && !health.error}
          />
          <HealthTile label="Sandbox backend" tone="success" value="docker" sub="hardened" />
          <HealthTile label="Econ plane" {...econTile} />
          <HealthTile label="Storage" tone="success" value="postgres" sub="+ redis" />
        </div>
      </Card>

      {/* Setup is a compact progress strip; operations then read as one full-width list. */}
      <div className="overview-content-stack">
        <ChecklistCard items={checklist} />
        <div className="overview-primary-column">
          <DeploymentsCard dep={dep} rollingBack={rollingBack} onRollback={doRollback} />
          <RecentRunsCard runs={runs} onOpen={() => router.push("/runs")} />
        </div>
      </div>
    </div>
  );
}

function HealthTile({
  label,
  tone,
  value,
  sub,
  loading,
}: {
  label: string;
  tone: string;
  value: string;
  sub: string;
  loading?: boolean;
}) {
  return (
    <div style={{ minWidth: 0, padding: "13px 15px", background: "var(--bg-card)" }}>
      <div style={{ fontSize: 11.5, fontWeight: 500, color: "var(--text-muted)", marginBottom: 7 }}>
        {label}
      </div>
      {loading ? (
        <>
          <Skeleton width={90} height={16} />
          <Skeleton width={60} height={10} style={{ marginTop: 8 }} />
        </>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
            <StatusDot tone={tone} />
            <span style={{ fontSize: 14, fontWeight: 500, color: "var(--text-primary)" }}>
              {value}
            </span>
          </div>
          <div
            style={{
              marginTop: 4,
              fontSize: 11.5,
              color: "var(--text-muted)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {sub}
          </div>
        </>
      )}
    </div>
  );
}

function DeploymentsCard({
  dep,
  rollingBack,
  onRollback,
}: {
  dep: Loadable<DeploymentSummary[]>;
  rollingBack: string | null;
  onRollback: (d: DeploymentSummary) => void;
}) {
  return (
    <Card label="Deployments" pad={16} style={{ minWidth: 0 }}>
      {dep.loading && !dep.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Skeleton height={38} />
          <Skeleton height={38} />
        </div>
      ) : dep.error ? (
        <ErrorNote message={dep.error} onRetry={dep.reload} />
      ) : !dep.data || dep.data.length === 0 ? (
        <div style={{ padding: "8px 0", fontSize: 13, color: "var(--text-muted)" }}>
          No deployments yet —{" "}
          <Link href="/studio" style={{ color: "var(--accent)", textDecoration: "none" }}>
            author one in Studio
          </Link>
          .
        </div>
      ) : (
        <div>
          {dep.data.map((d) => {
            const key = `${d.deployment_ref}@${d.version}`;
            const busy = rollingBack === key;
            return (
              <div key={key} className="overview-deployment-row">
                <div className="overview-deployment-summary">
                  <div
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: 13,
                      color: "var(--text-primary)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {d.deployment_ref}
                  </div>
                  <div
                    style={{
                      marginTop: 2,
                      fontFamily: "var(--font-mono)",
                      fontSize: 11,
                      color: "var(--text-faint)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {d.graph_version_ref}
                  </div>
                </div>
                <div className="overview-deployment-actions">
                  <Pill tone="accent">v{d.version}</Pill>
                  <Pill tone={d.serving ? "success" : "neutral"}>
                    {d.serving ? "serving" : "registered"}
                  </Pill>
                  <Button
                    variant="neutral"
                    disabled={busy}
                    onClick={() => onRollback(d)}
                    data-evidence-id={`overview.deployment.${d.deployment_ref}.version-${d.version}.rollback`}
                  >
                    {busy ? "…" : "Rollback"}
                  </Button>
                </div>
              </div>
            );
          })}
          <p
            style={{
              marginTop: 12,
              fontSize: 11.5,
              color: "var(--text-faint)",
              lineHeight: 1.5,
            }}
          >
            Creating / rolling back registers a version; serving requires restart.
          </p>
        </div>
      )}
    </Card>
  );
}

function ChecklistCard({ items }: { items: { label: string; done: boolean }[] }) {
  return (
    <Card label="Getting started" pad={16}>
      <div role="list" aria-label="Setup progress" className="overview-checklist-list">
        {items.map((it) => (
          <div
            key={it.label}
            role="listitem"
            aria-label={`${it.label}: ${it.done ? "completed" : "not completed"}`}
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <span
              aria-hidden
              style={{
                width: 15,
                height: 15,
                flexShrink: 0,
                borderRadius: 4,
                border: `1px solid ${it.done ? "var(--accent)" : "var(--hair-strong)"}`,
                background: it.done ? "var(--accent)" : "transparent",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--bg-page)",
                fontSize: 10,
                fontWeight: 700,
                lineHeight: 1,
              }}
            >
              {it.done ? "✓" : ""}
            </span>
            <span
              style={{
                fontSize: 13,
                color: it.done ? "var(--text-faint)" : "var(--text-secondary)",
              }}
            >
              {it.label}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function RecentRunsCard({ runs, onOpen }: { runs: Loadable<AdminRunList>; onOpen: () => void }) {
  return (
    <Card label="Recent runs" pad={16}>
      {runs.loading && !runs.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <Skeleton height={20} />
          <Skeleton height={20} />
          <Skeleton height={20} />
        </div>
      ) : runs.error ? (
        <ErrorNote message={runs.error} onRetry={runs.reload} />
      ) : !runs.data || runs.data.runs.length === 0 ? (
        <div style={{ padding: "6px 0", fontSize: 13, color: "var(--text-muted)" }}>No runs yet.</div>
      ) : (
        <div>
          {runs.data.runs.slice(0, 6).map((r) => (
            <button
              key={r.run_id}
              type="button"
              onClick={onOpen}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                width: "100%",
                textAlign: "left",
                background: "transparent",
                border: "none",
                borderTop: "1px solid var(--hair)",
                padding: "9px 4px",
                cursor: "pointer",
                color: "inherit",
              }}
            >
              <StatusDot tone={RUN_TONE[r.status] ?? "neutral"} pulse={r.status === "running"} />
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 12,
                  color: "var(--text-secondary)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  flexShrink: 0,
                  maxWidth: 240,
                }}
              >
                {r.run_id}
              </span>
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  color: "var(--text-faint)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  minWidth: 0,
                }}
              >
                {r.graph_version_ref}
              </span>
              <span style={{ marginLeft: "auto", flexShrink: 0 }}>
                <Pill tone={RUN_TONE[r.status] ?? "neutral"}>{r.status.replace(/_/g, " ")}</Pill>
              </span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}

function ErrorNote({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.3)",
        borderRadius: 6,
        padding: "10px 12px",
      }}
    >
      <span
        style={{
          fontSize: 12.5,
          color: "var(--danger)",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {message}
      </span>
      <Button variant="danger" onClick={onRetry} style={{ flexShrink: 0 }}>
        Retry
      </Button>
    </div>
  );
}
