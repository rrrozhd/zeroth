"use client";

// The Cost screen (handoff §6) — the govern-plane budget + attributed-spend view.
//
// Top row: three stat cards on a `repeat(3, minmax(0,1fr))` grid (the minmax(0,…)
// lets long env-var notes wrap instead of blowing out the track):
//   1. Month-to-date spend — the mono headline amount from getTenantCost(getTenant()),
//      a 6px progress bar of spend-vs-cap (teal fill on a #1a1f29 track, animated
//      width), and a "% of cap · tenant" line. All fields are real: total_cost_usd,
//      budget_cap_usd (nullable — "no cap set"), tenant_id.
//   2. Budget cap (USD) — a mono input + Set that PUTs { budget_cap_usd } via
//      setTenantBudget(getTenant(), body) (the sole TenantBudgetRequest field), then
//      reloads. Note covers the fail-open default / ZEROTH_REGULUS__FAIL_CLOSED=true.
//   3. Per-run ceiling — the documented $2.00 default with a note pointing at
//      ZEROTH_REGULUS__PER_RUN_CAP_USD. This is a deployment env knob, NOT a field on
//      TenantCostResponse, so it is framed as the config default, never as live data.
//
// Below: two cards.
//   - Spend by deployment — listDeployments() then getCostOf(ref) per unique
//     deployment_ref; each row is label + amount (total_cost_usd) + a 4px colored bar
//     whose width is that deployment's share of month-to-date tenant spend. Deployment
//     cost is cumulative while MTD is month-to-date, so the denominator is the larger of
//     the two — bars stay comparable and never overflow.
//   - Top nodes by attributed cost — getUnitEconomics() does NOT expose per-node cost,
//     so (per the handoff's "else show the workflow-level breakdown") this ranks
//     report.by_workflow by terminal_cost_usd. Nothing is fabricated.
//
// Every mutation toasts. Each loader owns its loading (Skeleton) / empty / inline-error
// (+ Retry) state, so an unconfigured or unreachable backend degrades gracefully and
// never crashes the screen. The API key is read only via lib/config — never logged,
// never placed in a URL.

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Card, NODE_TYPE_COLOR, Pill, Skeleton } from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  errMsg,
  getCostOf,
  getTenantCost,
  getUnitEconomics,
  listDeployments,
  setTenantBudget,
  type DeploymentSummary,
  type TenantBudgetRequest,
  type TenantCost,
  type UnitEconomicsReport,
} from "@/app/lib/api";
import { getTenant, isConfigured } from "@/app/lib/config";

const MONO = "var(--font-mono)";

// The design-doc default surfaced by the handoff. The effective ceiling is a
// deployment env var (ZEROTH_REGULUS__PER_RUN_CAP_USD) and is not carried on the
// cost API, so it is shown as a labeled default, not tenant-specific live data.
const PER_RUN_CEILING_DEFAULT = "$2.00";

// Distinct bar colors so deployments read apart at a glance (handoff palette).
const BAR_COLORS = [
  "var(--accent)",
  "var(--agent)",
  "var(--info)",
  "var(--success)",
  "var(--warning)",
  "var(--neutral)",
];

/** USD with 2 decimals at/above a dollar, 4 below — matches the Deployments/Runs
 *  screens so sub-cent attributed spend stays legible. */
function fmtUsd(n: number): string {
  if (!Number.isFinite(n)) return "$0.00";
  if (n === 0) return "$0.00";
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

/** Ratio (0..1) -> whole-percent string. */
function fmtRatioPct(r: number): string {
  return `${Math.round((Number.isFinite(r) ? r : 0) * 100)}%`;
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function CostPage() {
  const tenantCost = useLoad<TenantCost>(() => getTenantCost(getTenant()));
  const deployments = useLoad<DeploymentSummary[]>(listDeployments);
  const econ = useLoad<UnitEconomicsReport>(getUnitEconomics);

  // localStorage config is read after mount so the static prerender and first
  // client render agree (no hydration mismatch); loaders still fire on mount and
  // simply surface as errors when unconfigured — we render ConnectNote instead.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  // Bumped by Refresh to force the per-deployment cost effect to re-fetch even
  // when the deployment list itself is unchanged.
  const [refreshNonce, setRefreshNonce] = useState(0);

  const anyLoading = tenantCost.loading || deployments.loading || econ.loading;

  function refreshAll() {
    tenantCost.reload();
    deployments.reload();
    econ.reload();
    setRefreshNonce((n) => n + 1);
  }

  const mtd = tenantCost.data?.total_cost_usd ?? null;

  return (
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 22 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Cost</h1>
          <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
            Budget governance and attributed spend — enforced before LLM calls by the econ
            plane (/regulus).
          </p>
        </div>
        {connected && (
          <Button variant="neutral" onClick={refreshAll} disabled={anyLoading} style={{ flexShrink: 0 }}>
            {anyLoading ? "Refreshing…" : "Refresh"}
          </Button>
        )}
      </header>

      {!connected ? (
        <ConnectNote />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0,1fr))", gap: 12 }}>
            <MtdCard load={tenantCost} />
            <BudgetCard load={tenantCost} />
            <PerRunCeilingCard />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0,1fr))", gap: 12 }}>
            <SpendByDeployment deployments={deployments} mtd={mtd} refreshNonce={refreshNonce} />
            <TopNodesCard econ={econ} />
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// 1. Month-to-date spend
// --------------------------------------------------------------------------

function MtdCard({ load }: { load: Loadable<TenantCost> }) {
  return (
    <Card label="Month-to-date spend" pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Skeleton height={30} width={130} />
          <Skeleton height={6} />
          <Skeleton height={12} width={160} />
        </div>
      ) : load.error ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data ? (
        <MtdBody c={load.data} />
      ) : (
        <EmptyInline>No spend recorded.</EmptyInline>
      )}
    </Card>
  );
}

function MtdBody({ c }: { c: TenantCost }) {
  // Narrow to a positive number (or null) so arithmetic below is null-safe.
  const cap = c.budget_cap_usd != null && c.budget_cap_usd > 0 ? c.budget_cap_usd : null;
  const ratio = cap != null ? c.total_cost_usd / cap : 0;
  const over = cap != null && c.total_cost_usd > cap;

  return (
    <>
      <div
        style={{
          fontFamily: MONO,
          fontSize: 26,
          fontWeight: 600,
          lineHeight: 1.1,
          color: over ? "var(--danger)" : "var(--text-primary)",
        }}
      >
        {fmtUsd(c.total_cost_usd)}
      </div>

      <div
        style={{
          marginTop: 12,
          height: 6,
          borderRadius: 3,
          background: "#1a1f29",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${Math.max(0, Math.min(100, ratio * 100))}%`,
            background: over ? "var(--danger)" : "var(--accent)",
            borderRadius: 3,
            transition: "width 0.4s ease",
          }}
        />
      </div>

      <div
        style={{
          marginTop: 8,
          fontFamily: MONO,
          fontSize: 11,
          color: "var(--text-faint)",
          overflowWrap: "anywhere",
        }}
      >
        {cap != null ? `${fmtRatioPct(ratio)} of ${fmtUsd(cap)} cap` : "no cap set"} ·{" "}
        {c.tenant_id}
      </div>
    </>
  );
}

// --------------------------------------------------------------------------
// 2. Budget cap (USD)
// --------------------------------------------------------------------------

function BudgetCard({ load }: { load: Loadable<TenantCost> }) {
  const toast = useToast();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  // Keep the input synced to the server cap only until the operator starts typing.
  const touched = useRef(false);

  const serverCap = load.data?.budget_cap_usd ?? null;
  useEffect(() => {
    if (!touched.current) setValue(serverCap != null ? String(serverCap) : "");
  }, [serverCap]);

  async function submit() {
    const trimmed = value.trim();
    const n = Number(trimmed);
    if (!trimmed || !Number.isFinite(n) || n <= 0) {
      toast("Enter a positive USD budget cap.");
      return;
    }
    setBusy(true);
    try {
      const body: TenantBudgetRequest = { budget_cap_usd: n };
      await setTenantBudget(getTenant(), body);
      toast("Budget cap set — enforced pre-LLM via /regulus.");
      touched.current = false; // let the reloaded cap re-sync the field
      load.reload();
    } catch (e) {
      toast(`Set cap failed: ${errMsg(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card label="Budget cap (USD)" pad={16} style={{ minWidth: 0 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            flex: 1,
            minWidth: 0,
            background: "var(--bg-code)",
            border: "1px solid var(--hair-strong)",
            borderRadius: 6,
            paddingLeft: 10,
          }}
        >
          <span style={{ fontFamily: MONO, fontSize: 13, color: "var(--text-faint)" }}>$</span>
          <input
            value={value}
            onChange={(e) => {
              touched.current = true;
              setValue(e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !busy) submit();
            }}
            placeholder="e.g. 500"
            inputMode="decimal"
            aria-label="Budget cap in USD"
            style={{
              flex: 1,
              minWidth: 0,
              fontFamily: MONO,
              fontSize: 13,
              color: "var(--text-primary)",
              background: "transparent",
              border: "none",
              outline: "none",
              padding: "8px 10px 8px 6px",
            }}
          />
        </div>
        <Button variant="primary" onClick={submit} disabled={busy}>
          {busy ? "Setting…" : "Set"}
        </Button>
      </div>

      <div
        style={{
          marginTop: 8,
          fontSize: 11,
          color: "var(--text-faint)",
          lineHeight: 1.5,
          overflowWrap: "anywhere",
        }}
      >
        Fail-open by default — a Regulus outage will not block runs. Deny on an unavailable
        enforcement path with{" "}
        <code style={{ fontFamily: MONO, color: "var(--text-muted)" }}>
          ZEROTH_REGULUS__FAIL_CLOSED=true
        </code>
        .
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// 3. Per-run ceiling (config default — not a cost-API field)
// --------------------------------------------------------------------------

function PerRunCeilingCard() {
  return (
    <Card label="Per-run ceiling" pad={16} style={{ minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span style={{ fontFamily: MONO, fontSize: 26, fontWeight: 600 }}>
          {PER_RUN_CEILING_DEFAULT}
        </span>
        <Pill tone="neutral">default</Pill>
      </div>
      <div
        style={{
          marginTop: 12,
          fontSize: 11,
          color: "var(--text-faint)",
          lineHeight: 1.5,
          overflowWrap: "anywhere",
        }}
      >
        Set per deployment via{" "}
        <code style={{ fontFamily: MONO, color: "var(--text-muted)" }}>
          ZEROTH_REGULUS__PER_RUN_CAP_USD
        </code>{" "}
        — locally enforced from audited run cost, independent of the tenant cap.
      </div>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Spend by deployment
// --------------------------------------------------------------------------

function SpendByDeployment({
  deployments,
  mtd,
  refreshNonce,
}: {
  deployments: Loadable<DeploymentSummary[]>;
  mtd: number | null;
  refreshNonce: number;
}) {
  const list = deployments.data ?? [];
  // One row per unique deployment_ref — getCostOf is ref-scoped (cumulative across
  // that ref's versions), and the list can carry several versions per ref.
  const refs = useMemo(() => Array.from(new Set(list.map((d) => d.deployment_ref))), [list]);
  const refsKey = refs.join("|");

  // undefined = not fetched yet, null = fetch failed for that ref, number = cost.
  const [costs, setCosts] = useState<Record<string, number | null>>({});
  const [costLoading, setCostLoading] = useState(false);

  useEffect(() => {
    const refList = refsKey ? refsKey.split("|") : [];
    if (refList.length === 0) {
      setCosts({});
      setCostLoading(false);
      return;
    }
    let cancelled = false;
    setCostLoading(true);
    Promise.allSettled(refList.map((r) => getCostOf(r)))
      .then((results) => {
        if (cancelled) return;
        const next: Record<string, number | null> = {};
        refList.forEach((r, i) => {
          const res = results[i];
          next[r] = res.status === "fulfilled" ? res.value.total_cost_usd : null;
        });
        setCosts(next);
      })
      .finally(() => {
        if (!cancelled) setCostLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refsKey, refreshNonce]);

  // Denominator for the share bar: the larger of month-to-date tenant spend and the
  // summed cumulative deployment cost, so bars never overflow and stay comparable.
  const known = refs.map((r) => costs[r]).filter((v): v is number => v != null);
  const sumCosts = known.reduce((a, b) => a + b, 0);
  const denom = Math.max(mtd ?? 0, sumCosts, 1e-9);

  const ordered = [...refs].sort((a, b) => (costs[b] ?? -1) - (costs[a] ?? -1));

  return (
    <Card label="Spend by deployment" pad={16} style={{ minWidth: 0 }}>
      <p style={{ margin: "0 0 12px", fontSize: 11.5, color: "var(--text-muted)", lineHeight: 1.5 }}>
        Cumulative attributed cost per deployment; the bar is its share of month-to-date tenant
        spend.
      </p>

      {deployments.loading && !deployments.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <Skeleton height={14} />
              <Skeleton height={4} />
            </div>
          ))}
        </div>
      ) : deployments.error ? (
        <InlineError message={deployments.error} onRetry={deployments.reload} />
      ) : ordered.length === 0 ? (
        <EmptyInline>
          No deployments yet — register one in{" "}
          <Link href="/deployments" style={{ color: "var(--accent)", textDecoration: "none" }}>
            Deployments
          </Link>
          .
        </EmptyInline>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {ordered.map((ref, i) => {
            const cost = costs[ref]; // undefined | null | number
            const width = typeof cost === "number" ? Math.min(100, (cost / denom) * 100) : 0;
            return (
              <div key={ref}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                  <span
                    title={ref}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      fontFamily: MONO,
                      fontSize: 12,
                      color: "var(--text-primary)",
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {ref}
                  </span>
                  <span
                    style={{
                      fontFamily: MONO,
                      fontSize: 12,
                      color: cost == null ? "var(--text-faint)" : "var(--text-secondary)",
                      flexShrink: 0,
                    }}
                  >
                    {cost === undefined
                      ? costLoading
                        ? "…"
                        : "—"
                      : cost === null
                        ? "—"
                        : fmtUsd(cost)}
                  </span>
                </div>
                <div
                  style={{
                    marginTop: 6,
                    height: 4,
                    borderRadius: 2,
                    background: "#1a1f29",
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: `${width}%`,
                      background: BAR_COLORS[i % BAR_COLORS.length],
                      borderRadius: 2,
                      transition: "width 0.4s ease",
                    }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Top nodes by attributed cost — workflow-level, since getUnitEconomics()
// attributes cost by workflow (by_workflow[].terminal_cost_usd), not per node.
// --------------------------------------------------------------------------

type WorkflowRow = UnitEconomicsReport["by_workflow"][number];

function TopNodesCard({ econ }: { econ: Loadable<UnitEconomicsReport> }) {
  return (
    <Card label="Top nodes by attributed cost" pad={16} style={{ minWidth: 0 }}>
      <p style={{ margin: "0 0 12px", fontSize: 11.5, color: "var(--text-muted)", lineHeight: 1.5 }}>
        Attributed at the workflow level — per-node cost is not exposed by unit economics, so
        these are workflow totals over the last window.
      </p>

      {econ.loading && !econ.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} height={18} />
          ))}
        </div>
      ) : econ.error ? (
        <InlineError message={econ.error} onRetry={econ.reload} />
      ) : econ.data ? (
        <TopNodesBody report={econ.data} />
      ) : (
        <EmptyInline>No unit-economics data.</EmptyInline>
      )}
    </Card>
  );
}

function TopNodesBody({ report }: { report: UnitEconomicsReport }) {
  const rows: WorkflowRow[] = [...(report.by_workflow ?? [])]
    .filter((w) => w.terminal_cost_usd > 0)
    .sort((a, b) => b.terminal_cost_usd - a.terminal_cost_usd)
    .slice(0, 8);

  if (rows.length === 0) {
    return <EmptyInline>{report.note || "No attributed cost yet."}</EmptyInline>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {rows.map((w, i) => (
        <div
          key={w.workflow_name}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "9px 0",
            borderTop: i === 0 ? "none" : "1px solid var(--hair)",
          }}
        >
          <span
            aria-hidden
            style={{
              width: 8,
              height: 8,
              borderRadius: 2,
              flexShrink: 0,
              background: NODE_TYPE_COLOR.subgraph,
            }}
          />
          <span
            title={w.workflow_name}
            style={{
              fontFamily: MONO,
              fontSize: 12,
              color: "var(--text-primary)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              minWidth: 0,
              flexShrink: 1,
            }}
          >
            {w.workflow_name}
          </span>
          <span
            style={{
              flex: 1,
              minWidth: 0,
              fontFamily: MONO,
              fontSize: 11,
              color: "var(--text-faint)",
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {w.runs} run{w.runs === 1 ? "" : "s"} · {fmtRatioPct(w.success_rate)} ok
          </span>
          <span
            style={{
              fontFamily: MONO,
              fontSize: 12,
              color: "var(--text-secondary)",
              flexShrink: 0,
            }}
          >
            {fmtUsd(w.terminal_cost_usd)}
          </span>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirror the Audit / Deployments screens)
// --------------------------------------------------------------------------

function ConnectNote() {
  return (
    <Card pad={20}>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Not connected. Open <span style={{ color: "var(--accent)" }}>Connect</span> (bottom-left) to
        set the API base and key.
      </div>
    </Card>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12.5, color: "var(--text-faint)", lineHeight: 1.55 }}>{children}</div>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.3)",
        borderRadius: 8,
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
