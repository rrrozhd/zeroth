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
//      reloads. Its note reflects the effective runtime failure mode rather than
//      presenting a design default as live configuration.
//   3. Per-run ceiling — the effective value returned by the scoped runtime
//      configuration endpoint. An unavailable or unconfigured value is labeled as
//      such; this screen never substitutes a design-document default.
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
import {
  Button,
  ConsoleField,
  ConsoleInput,
  ConsoleMeta,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  Skeleton,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { EconomicsWorkspaceNav } from "@/app/components/EconomicsWorkspaceNav";
import { fmtUsd } from "@/app/components/ui";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  errMsg,
  getCostOf,
  getEconomicsConfiguration,
  getTenantCost,
  getUnitEconomics,
  listDeployments,
  setTenantBudget,
  type DeploymentSummary,
  type EconomicsConfiguration,
  type TenantBudgetRequest,
  type TenantCost,
  type UnitEconomicsReport,
} from "@/app/lib/api";
import { getTenant, isConfigured } from "@/app/lib/config";
import { isForbiddenSurface, surfaceAccessMessage } from "@/app/lib/surfaceAccess";
import styles from "./cost.module.css";
import { budgetFailureModeCopy, reconcileEconomics } from "./economicsTruth";


/** Ratio (0..1) with enough precision to keep small, real spend visible. */
function fmtRatioPct(r: number): string {
  const pct = (Number.isFinite(r) ? r : 0) * 100;
  if (pct === 0) return "0%";
  if (Math.abs(pct) < 0.1) return pct > 0 ? "<0.1%" : ">-0.1%";
  if (Math.abs(pct) < 10) return `${pct.toFixed(1).replace(/\.0$/, "")}%`;
  return `${Math.round(pct)}%`;
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function CostPage() {
  const tenantCost = useLoad<TenantCost>(() => getTenantCost(getTenant()));
  const configuration = useLoad<EconomicsConfiguration>(getEconomicsConfiguration);
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

  const anyLoading = tenantCost.loading || configuration.loading || deployments.loading || econ.loading;

  function refreshAll() {
    tenantCost.reload();
    configuration.reload();
    deployments.reload();
    econ.reload();
    setRefreshNonce((n) => n + 1);
  }

  const mtd = tenantCost.data?.actual_spend_usd ?? null;

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Economics"
        description="Spend, budgets, unit economics, cost models, and reconciliation in one governed workspace."
        actions={connected ? (
          <Button variant="neutral" onClick={refreshAll} disabled={anyLoading}>
            {anyLoading ? "Refreshing…" : "Refresh"}
          </Button>
        ) : undefined}
      />

      <EconomicsWorkspaceNav active="spend" />

      {!connected ? (
        <ConnectNote />
      ) : (
        <div className={styles.stack}>
          <ConsoleSection
            title="Budget exposure"
            meta="Tenant scope · production ledger · month to date"
          >
            <ConsoleSurface density="flush">
            <div className={styles.controlBand}>
              <MtdCard load={tenantCost} />
              <ExposureCard load={tenantCost} />
              <BudgetCard load={tenantCost} configuration={configuration} />
              <PerRunCeilingCard load={configuration} />
            </div>
            </ConsoleSurface>
          </ConsoleSection>
          {tenantCost.data && tenantCost.data.synthetic_control_usd > 0 ? (
            <ConsoleNotice title="Control proofs excluded">
              {fmtUsd(tenantCost.data.synthetic_control_usd)} belongs to synthetic budget-gate
              verification. It remains inspectable evidence but is excluded from actual provider
              spend, deployment attribution, and budget consumption.
            </ConsoleNotice>
          ) : null}
          <div className={styles.pairGrid}>
            <SpendByDeployment deployments={deployments} mtd={mtd} refreshNonce={refreshNonce} />
            <TopNodesCard econ={econ} tenantCost={tenantCost} />
          </div>
        </div>
      )}
    </ConsolePage>
  );
}

// --------------------------------------------------------------------------
// 1. Month-to-date spend
// --------------------------------------------------------------------------

function MtdCard({ load }: { load: Loadable<TenantCost> }) {
  return (
    <BudgetCell label="Actual provider spend">
      {load.loading && !load.data ? (
        <div className={styles.loadingStack}>
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
    </BudgetCell>
  );
}

function MtdBody({ c }: { c: TenantCost }) {
  // Narrow to a positive number (or null) so arithmetic below is null-safe.
  const cap = c.budget_cap_usd != null && c.budget_cap_usd > 0 ? c.budget_cap_usd : null;
  const ratio = cap != null ? c.budget_consumed_usd / cap : 0;
  const over = cap != null && c.budget_consumed_usd > cap;

  return (
    <>
      <div className={`${styles.metricValue} ${over ? styles.metricDanger : ""}`}>
        {fmtUsd(c.actual_spend_usd)}
      </div>

      <div className={styles.progressTrack}>
        <div
          className={`${styles.progressFill} ${over ? styles.progressFillDanger : ""}`}
          style={{
            transform: `scaleX(${Math.max(0, Math.min(1, ratio))})`,
          }}
        />
      </div>

      <div className={styles.controlMeta}>
        {cap != null ? `${fmtRatioPct(ratio)} of ${fmtUsd(cap)} cap` : "no cap set"} ·{" "}
        {c.tenant_id}
      </div>
      <div className={styles.controlMeta}>
        {fmtUsd(c.paid_spend_usd)} measured · {fmtUsd(c.estimated_spend_usd)} estimated<br />
        Budget consumed: {fmtUsd(c.budget_consumed_usd)} including open exposure
      </div>
    </>
  );
}

function ExposureCard({ load }: { load: Loadable<TenantCost> }) {
  const active = load.data?.active_exposure_usd ?? 0;
  const ambiguous = load.data?.ambiguous_exposure_usd ?? 0;
  return (
    <BudgetCell label="Reserved exposure">
      {load.loading && !load.data ? (
        <Skeleton height={30} width={130} />
      ) : load.error ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data ? (
        <>
          <div className={styles.metricValue}>{fmtUsd(active + ambiguous)}</div>
          <div className={styles.controlMeta}>
            {fmtUsd(active)} active · {fmtUsd(ambiguous)} ambiguous<br />
            Reserved or unresolved maxima · not yet spend
          </div>
        </>
      ) : (
        <EmptyInline>Not measured</EmptyInline>
      )}
    </BudgetCell>
  );
}

// --------------------------------------------------------------------------
// 2. Budget cap (USD)
// --------------------------------------------------------------------------

function BudgetCard({
  load,
  configuration,
}: {
  load: Loadable<TenantCost>;
  configuration: Loadable<EconomicsConfiguration>;
}) {
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
    <BudgetCell label="Budget cap (USD)">
      <div className={styles.controlForm}>
        <ConsoleField label="Amount">
          <ConsoleInput
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
          />
        </ConsoleField>
        <Button variant="primary" onClick={submit} disabled={busy}>
          {busy ? "Setting…" : "Set"}
        </Button>
      </div>

      <div className={styles.controlMeta}>
        {budgetFailureModeCopy(configuration.data?.failure_mode)}
      </div>
    </BudgetCell>
  );
}

// --------------------------------------------------------------------------
// 3. Per-run ceiling (effective service-runtime configuration)
// --------------------------------------------------------------------------

function PerRunCeilingCard({ load }: { load: Loadable<EconomicsConfiguration> }) {
  const restricted = load.error != null && load.error.startsWith("403");
  return (
    <BudgetCell label="Per-run ceiling">
      {load.loading && !load.data ? (
        <Skeleton height={30} width={130} />
      ) : load.error ? (
        <div className={styles.stateBlock}>
          <strong>{restricted ? "Access restricted" : "Fetch failed"}</strong>
          <span>
            {restricted
              ? "This API key cannot read effective economics configuration. Connect with a metrics-read credential."
              : load.error}
          </span>
          {!restricted && <Button variant="neutral" onClick={load.reload}>Retry</Button>}
        </div>
      ) : load.data ? (
        <>
          <div className={styles.metricLine}>
            <span className={styles.metricValue}>
              {load.data.per_run_cap_usd == null ? "Not configured" : fmtUsd(load.data.per_run_cap_usd)}
            </span>
            <ConsoleMeta>{load.data.failure_mode.replace("_", "-")}</ConsoleMeta>
          </div>
          <div className={styles.controlMeta}>
            Scope: {load.data.tenant_id} / {load.data.deployment_ref}<br />
            Source: service runtime · Freshness: current request
          </div>
        </>
      ) : (
        <EmptyInline>Not measured</EmptyInline>
      )}
      <div className={styles.controlMeta}>
        This is the effective runtime value, not a documented default.
      </div>
    </BudgetCell>
  );
}

function BudgetCell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className={styles.controlCell}>
      <h3 className={styles.controlLabel}>{label}</h3>
      {children}
    </section>
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

  // undefined = not measured yet, null = fetch failed for that ref, number = measured cost.
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
  const visible = ordered.filter((ref) => costs[ref] == null || (costs[ref] ?? 0) > 0);
  const zeroCostCount = ordered.length - visible.length;

  return (
    <ConsoleSection title="Actual spend by deployment">
      <ConsoleSurface>
      <p className={styles.sectionNote}>
        Committed production cost per deployment; the bar is its share of month-to-date actual
        spend. Control proofs are excluded. Scope: connected tenant · Source: production cost ledger ·
        Window: month to date · Freshness: current request.
      </p>

      {deployments.loading && !deployments.data ? (
        <div className={styles.loadingStack}>
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className={styles.loadingStack}>
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
          <Link href="/deployments" className={styles.link}>
            Deployments
          </Link>
          .
        </EmptyInline>
      ) : (
        <div className={styles.deploymentList}>
          {visible.map((ref) => {
            const cost = costs[ref]; // undefined | null | number
            const width = typeof cost === "number" ? Math.min(100, (cost / denom) * 100) : 0;
            return (
              <div key={ref} className={styles.deploymentRow}>
                <div className={styles.deploymentLine}>
                  <span
                    title={ref}
                    className={styles.deploymentName}
                  >
                    {ref}
                  </span>
                  <span
                    className={styles.deploymentCost}
                    style={{ color: cost == null ? "var(--text-faint)" : undefined }}
                  >
                    {cost === undefined
                      ? costLoading
                        ? "Loading…"
                        : "Not measured"
                      : cost === null
                        ? "Fetch failed"
                        : fmtUsd(cost)}
                  </span>
                </div>
                <div className={styles.progressTrack}>
                  <div
                    className={styles.progressFill}
                    style={{
                      transform: `scaleX(${Math.max(0, Math.min(100, width)) / 100})`,
                    }}
                  />
                </div>
              </div>
            );
          })}
          {zeroCostCount > 0 ? (
            <p className={styles.zeroCostSummary}>
              {zeroCostCount} other {zeroCostCount === 1 ? "deployment has" : "deployments have"}{" "}
              no recorded provider spend in the production ledger.
            </p>
          ) : null}
        </div>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

// --------------------------------------------------------------------------
// Top nodes by attributed cost — workflow-level, since getUnitEconomics()
// attributes cost by workflow (by_workflow[].terminal_cost_usd), not per node.
// --------------------------------------------------------------------------

type WorkflowRow = UnitEconomicsReport["by_workflow"][number];

function TopNodesCard({
  econ,
  tenantCost,
}: {
  econ: Loadable<UnitEconomicsReport>;
  tenantCost: Loadable<TenantCost>;
}) {
  return (
    <ConsoleSection title="Run-attributed economics">
      <ConsoleSurface>
      <p className={styles.sectionNote}>
        Workflow totals for the latest unit-economics window. Per-node cost is not exposed.
        {" "}Scope: connected tenant · Window: latest 200 runs · Source: audit and run records ·
        Freshness: current request.
      </p>

      {econ.loading && !econ.data ? (
        <div className={styles.loadingStack}>
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} height={18} />
          ))}
        </div>
      ) : econ.error ? (
        <InlineError message={econ.error} onRetry={econ.reload} />
      ) : econ.data ? (
        <TopNodesBody
          report={econ.data}
          ledgerActualUsd={tenantCost.data?.actual_spend_usd ?? null}
        />
      ) : (
        <EmptyInline>No unit-economics data.</EmptyInline>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

function TopNodesBody({
  report,
  ledgerActualUsd,
}: {
  report: UnitEconomicsReport;
  ledgerActualUsd: number | null;
}) {
  const rows: WorkflowRow[] = [...(report.by_workflow ?? [])]
    .filter(
      (row) =>
        row.terminal_cost_usd > 0 || row.estimated_terminal_cost_usd > 0,
    )
    .sort(
      (a, b) =>
        Math.max(b.terminal_cost_usd, b.estimated_terminal_cost_usd) -
        Math.max(a.terminal_cost_usd, a.estimated_terminal_cost_usd),
    )
    .slice(0, 8);

  const reconciliation = ledgerActualUsd == null
    ? null
    : reconcileEconomics(ledgerActualUsd, report.total_cost_usd);

  if (rows.length === 0) {
    return (
      <div className={styles.reconciliationStack}>
        <p className={styles.emptyTitle}>No priced workflow runs in this window</p>
        <EmptyInline>{report.note || "No attributed run cost yet."}</EmptyInline>
        {reconciliation ? <ReconciliationSummary value={reconciliation} /> : null}
      </div>
    );
  }

  return (
    <div className={styles.reconciliationStack}>
      <div className={styles.workflowList}>
        {rows.map((w) => (
          <div
            key={w.workflow_name}
            className={styles.workflowRow}
          >
            <span
              title={w.workflow_name}
              className={styles.workflowName}
            >
              {w.workflow_name}
            </span>
            <span className={styles.workflowMeta}>
              {w.runs} run{w.runs === 1 ? "" : "s"} · {fmtRatioPct(w.success_rate)} ok
            </span>
            <span className={styles.workflowCost}>
              {w.terminal_cost_usd > 0
                ? `${fmtUsd(w.terminal_cost_usd)} measured`
                : null}
              {w.terminal_cost_usd > 0 && w.estimated_terminal_cost_usd > 0
                ? " · "
                : null}
              {w.estimated_terminal_cost_usd > 0
                ? `${fmtUsd(w.estimated_terminal_cost_usd)} estimated`
                : null}
            </span>
          </div>
        ))}
      </div>
      {reconciliation ? <ReconciliationSummary value={reconciliation} /> : null}
    </div>
  );
}

function ReconciliationSummary({
  value,
}: {
  value: ReturnType<typeof reconcileEconomics>;
}) {
  return (
    <div className={styles.reconciliation} data-evidence-id="economics-run-ledger-reconciliation">
      <div className={styles.reconciliationLine}>
        <span>Month-to-date production ledger</span>
        <strong>{fmtUsd(value.ledgerActualUsd)}</strong>
      </div>
      <div className={styles.reconciliationLine}>
        <span>Latest-window run attribution</span>
        <strong>{fmtUsd(value.runAttributedUsd)}</strong>
      </div>
      <div className={styles.reconciliationLine}>
        <span>Window / operation difference</span>
        <strong>{fmtUsd(value.differenceUsd)}</strong>
      </div>
      <p>{value.explanation}</p>
    </div>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirror the Audit / Deployments screens)
// --------------------------------------------------------------------------

function ConnectNote() {
  return (
    <ConsoleNotice title="Not connected">
      Open Connect from the navigation to set the API base and key.
    </ConsoleNotice>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <p className={styles.inlineEmpty}>{children}</p>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  const restricted = isForbiddenSurface(message);
  return (
    <ConsoleNotice
      tone={restricted ? "neutral" : "danger"}
      title={restricted ? "Access restricted" : "Cost data unavailable"}
      actions={restricted ? undefined : <Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {surfaceAccessMessage(message, "Cost data")}
    </ConsoleNotice>
  );
}
