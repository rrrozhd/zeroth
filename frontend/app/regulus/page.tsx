"use client";

// Regulus — Econ Dashboard (platform-admin econ control-plane view).
//
// This is the GLOBAL econ view (across all tenants), reached only through the
// admin-gated Regulus proxy (`/v1/econ/regulus/*`, see lib/regulusApi.ts). Every
// panel owns its own useLoad, so the screen degrades panel-by-panel — Skeleton on
// first paint, an inline error + Retry on a real failure, and a plain "no data
// yet" on an empty (but successful) response. A fresh econ DB returns real,
// all-zero payloads: those render as zeros, never as errors. The API key is
// attached only inside lib/api's apiFetch (X-API-Key header) — never logged, never
// placed in a URL.
//
// Layout (top → bottom):
//   - KPI row      — 5 stat tiles from rgKpis(): spend, value, net margin,
//                    portfolio confidence, efficiency index.
//   - Trends       — 4 compact cards, each an inline SVG sparkline (single series,
//                    var(--accent), 120x32 viewBox) over a TrendPoint[] {x,y}.
//   - Rankings     — top-creators + capital-destroyers (CapabilityValueRow[]) as
//                    two tables, then capability-ranking (CapabilityRankingRow[]).
//   - Gate + mix   — confidence-gate (passed/blocked) and data-quality mix
//                    (measured/inferred/mixed) as proportion cards.
//   - Policy       — enforcement policy timeline (PolicyTimelineRow[]) as a table.

import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  Button,
  ConsoleMetric,
  ConsoleMetricBand,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import { useRegulus } from "@/app/components/regulusContext";
import { EconomicsWorkspaceNav } from "@/app/components/EconomicsWorkspaceNav";
import { fmtUsd } from "@/app/components/ui";
import { isConfigured } from "@/app/lib/config";
import {
  rgActionSuppression,
  rgCalibrationTrend,
  rgCapabilityRanking,
  rgCapitalDestroyers,
  rgConfidenceGate,
  rgConfidenceTrend,
  rgDataQualityMix,
  rgEfficiencyTrend,
  rgKpis,
  rgPolicyTimeline,
  rgTopCreators,
  type CapabilityRankingRow,
  type CapabilityValueRow,
  type ConfidenceGateStatus,
  type DataQualityMix,
  type KPIResponse,
  type PolicyTimelineRow,
  type TrendPoint,
} from "@/app/lib/regulusApi";
import styles from "./economics.module.css";


// --------------------------------------------------------------------------
// Formatters (all client-side — panels only render after a client fetch, so
// locale formatting can't cause a hydration mismatch).
// --------------------------------------------------------------------------

/** Compact number: thousands-separated, ≤2 decimals, tiny values → exponent. */
function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs !== 0 && abs < 0.01) return n.toExponential(1);
  return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

/** Whole-percent from a 0..1 ratio. */
function fmtPct(ratio: number): string {
  return `${Math.round((Number.isFinite(ratio) ? ratio : 0) * 100)}%`;
}

/** portfolio_confidence_score is conventionally a 0..1 score → show as %; if a
 *  backend ever returns it out of that range, fall back to a raw number rather
 *  than a misleading percentage. */
function fmtScore(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return n >= 0 && n <= 1 ? `${Math.round(n * 100)}%` : fmtNum(n);
}

function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

/** Short caption for a sparkline's latest x — a date if parseable, else the raw
 *  label (truncated). */
function shortX(x: string): string {
  const d = new Date(x);
  if (!Number.isNaN(d.getTime())) return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return x.length > 12 ? `${x.slice(0, 12)}…` : x;
}

function marginColor(n: number): string {
  if (!Number.isFinite(n) || n === 0) return "var(--text-secondary)";
  return n > 0 ? "var(--success)" : "var(--danger)";
}

// capability_type (REVENUE | COST | RISK | PRODUCTIVITY and their long forms).
const CAP_TYPE_TONE: Record<string, string> = {
  REVENUE: "success",
  COST: "warning",
  COSTREDUCTION: "warning",
  RISK: "danger",
  RISKMITIGATION: "danger",
  PRODUCTIVITY: "info",
};
function capTypeTone(t: string): string {
  return CAP_TYPE_TONE[t.toUpperCase().replace(/[^A-Z]/g, "")] ?? "neutral";
}

// policy status (PROPOSED | APPROVED | REJECTED | APPLIED | FAILED).
const POLICY_TONE: Record<string, string> = {
  PROPOSED: "info",
  APPROVED: "success",
  APPLIED: "accent",
  REJECTED: "danger",
  FAILED: "danger",
};
function policyTone(s: string): string {
  return POLICY_TONE[s.toUpperCase()] ?? "neutral";
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function RegulusEconDashboard() {
  const kpis = useLoad<KPIResponse>(rgKpis);
  const topCreators = useLoad<CapabilityValueRow[]>(rgTopCreators);
  const destroyers = useLoad<CapabilityValueRow[]>(rgCapitalDestroyers);
  const ranking = useLoad<CapabilityRankingRow[]>(rgCapabilityRanking);
  const confTrend = useLoad<TrendPoint[]>(rgConfidenceTrend);
  const effTrend = useLoad<TrendPoint[]>(rgEfficiencyTrend);
  const calibTrend = useLoad<TrendPoint[]>(rgCalibrationTrend);
  const suppTrend = useLoad<TrendPoint[]>(rgActionSuppression);
  const gate = useLoad<ConfidenceGateStatus>(rgConfidenceGate);
  const dqMix = useLoad<DataQualityMix>(rgDataQualityMix);
  const policy = useLoad<PolicyTimelineRow[]>(rgPolicyTimeline);

  const all: Loadable<unknown>[] = [
    kpis,
    topCreators,
    destroyers,
    ranking,
    confTrend,
    effTrend,
    calibTrend,
    suppTrend,
    gate,
    dqMix,
    policy,
  ];
  const anyLoading = all.some((l) => l.loading);
  function reloadAll() {
    all.forEach((l) => l.reload());
  }

  // Read localStorage only after mount so the static prerender and first client
  // render agree (no hydration mismatch); loaders still fire on mount.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  const reg = useRegulus();

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Economics"
        description="Platform economic control plane across tenants."
        actions={connected ? (
          <Button variant="neutral" onClick={reloadAll} disabled={anyLoading}>
            {anyLoading ? "Refreshing…" : "Refresh"}
          </Button>
        ) : undefined}
      />

      <EconomicsWorkspaceNav active="workflows" />

      <ConsoleNotice title="Data context">
        Scope: platform-wide across authorized tenants · Window: latest control-plane snapshot ·
        Source: Regulus · Freshness: current request.
      </ConsoleNotice>

      <ConsoleNotice title="Valuation model, not the spend ledger">
        Regulus totals come from explicit cost/value estimates and outcome valuations. They do not
        mirror production provider charges from Spend &amp; budgets. No synthetic or measured outcomes
        have been valued in this campaign yet, so value and margin correctly remain zero.
      </ConsoleNotice>

      {!connected ? (
        <ConnectNote />
      ) : reg === "absent" ? (
        <EconAbsentNote />
      ) : (
        <div className={styles.stack}>
          <KpiRow load={kpis} />

          <div className={styles.trendGrid}>
            <TrendPanel label="Confidence" load={confTrend} />
            <TrendPanel label="Efficiency" load={effTrend} />
            <TrendPanel label="Calibration" load={calibTrend} />
            <TrendPanel label="Action suppression" load={suppTrend} />
          </div>

          <div className={styles.pairGrid}>
            <ValueRankPanel label="Top creators" load={topCreators} order="desc" />
            <ValueRankPanel label="Capital destroyers" load={destroyers} order="asc" />
          </div>

          <CapabilityRankingPanel load={ranking} />

          <div className={styles.pairGrid}>
            <ConfidenceGatePanel load={gate} />
            <DataQualityPanel load={dqMix} />
          </div>

          <PolicyTimelinePanel load={policy} />
        </div>
      )}
    </ConsolePage>
  );
}

// --------------------------------------------------------------------------
// KPI row
// --------------------------------------------------------------------------

function KpiRow({ load }: { load: Loadable<KPIResponse> }) {
  if (load.loading && !load.data) {
    return (
      <div className={styles.loadingBand}>
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className={styles.loadingMetric}>
              <Skeleton height={11} width={90} />
              <Skeleton height={26} width={120} />
            </div>
          ))}
      </div>
    );
  }
  if (load.error && !load.data) {
    return (
      <InlineError message={load.error} onRetry={load.reload} />
    );
  }
  const k = load.data;
  if (!k) {
    return (
      <ConsoleSurface><Empty>No KPIs yet.</Empty></ConsoleSurface>
    );
  }

  const tiles: { label: string; value: string; tone?: "default" | "danger" | "success" }[] = [
    { label: "Valued execution cost", value: fmtUsd(k.total_ai_spend_usd) },
    { label: "Recorded outcome value", value: fmtUsd(k.total_ai_value_usd) },
    { label: "Net AI margin", value: fmtUsd(k.net_ai_margin_usd), tone: k.net_ai_margin_usd > 0 ? "success" : k.net_ai_margin_usd < 0 ? "danger" : "default" },
    { label: "Portfolio confidence", value: fmtScore(k.portfolio_confidence_score) },
    { label: "Efficiency index", value: fmtNum(k.efficiency_index) },
  ];

  return (
    <ConsoleMetricBand columns={5} ariaLabel="Economics totals">
        {tiles.map((t) => (
          <ConsoleMetric key={t.label} label={t.label} value={t.value} tone={t.tone} />
        ))}
    </ConsoleMetricBand>
  );
}

// --------------------------------------------------------------------------
// Trends — inline SVG sparklines
// --------------------------------------------------------------------------

function TrendPanel({ label, load }: { label: string; load: Loadable<TrendPoint[]> }) {
  return (
    <ConsoleSection title={label} className={styles.panel}>
      <ConsoleSurface>
      {load.loading && !load.data ? (
        <div>
          <Skeleton height={20} width={70} />
          <div className={styles.chart}>
            <Skeleton height={32} />
          </div>
        </div>
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <TrendBody points={load.data} />
      ) : (
        <Empty>No data yet.</Empty>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

function TrendBody({ points }: { points: TrendPoint[] }) {
  const latest = points[points.length - 1];
  return (
    <div>
      <div className={styles.trendHeader}>
        <span className={styles.trendValue}>
          {fmtNum(latest.y)}
        </span>
        <span className={styles.trendDate}>
          {shortX(latest.x)}
        </span>
      </div>
      <div className={styles.chart}>
        <Sparkline points={points} />
      </div>
    </div>
  );
}

/** Single-series sparkline. Coordinates are computed against a fixed 120x32
 *  viewBox and the <svg> stretches to the card width (preserveAspectRatio="none");
 *  vectorEffect="non-scaling-stroke" keeps the stroke crisp under that scaling.
 *  < 2 finite points → a dashed baseline so the card still reads as a chart. */
function Sparkline({ points }: { points: TrendPoint[] }) {
  const W = 120;
  const H = 32;
  const pad = 3;
  const ys = points.map((p) => p.y).filter((y): y is number => Number.isFinite(y));

  if (ys.length < 2) {
    return (
      <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="sparkline">
        <line
          x1={pad}
          y1={H / 2}
          x2={W - pad}
          y2={H / 2}
          stroke="var(--hair-strong)"
          strokeWidth={1}
          strokeDasharray="2 3"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    );
  }

  const min = ys.reduce((a, b) => Math.min(a, b), Infinity);
  const max = ys.reduce((a, b) => Math.max(a, b), -Infinity);
  const flat = max === min;
  const dx = (W - pad * 2) / (ys.length - 1);
  const pts = ys
    .map((y, i) => {
      const x = pad + i * dx;
      const yy = flat ? H / 2 : H - pad - ((y - min) / (max - min)) * (H - pad * 2);
      return `${x.toFixed(1)},${yy.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="sparkline">
      <polyline
        points={pts}
        fill="none"
        stroke="var(--accent)"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

// --------------------------------------------------------------------------
// Rankings
// --------------------------------------------------------------------------

function ValueRankPanel({
  label,
  load,
  order,
}: {
  label: string;
  load: Loadable<CapabilityValueRow[]>;
  order: "asc" | "desc";
}) {
  return (
    <ConsoleSection title={label} className={styles.panel}>
      <ConsoleSurface>
      {load.loading && !load.data ? (
        <SkeletonRows n={5} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <MiniTable<CapabilityValueRow>
          ariaLabel={`${label} capabilities`}
          evidenceId={`regulus.table.${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`}
          minWidth={280}
          rows={[...load.data]
            .sort((a, b) => (order === "asc" ? a.net_margin_usd - b.net_margin_usd : b.net_margin_usd - a.net_margin_usd))
            .slice(0, 8)}
          rowKey={(r) => r.capability_id}
          cols={[
            {
              header: "Capability",
              render: (r) => <Trunc title={r.capability_id}>{r.capability_id}</Trunc>,
            },
            {
              header: "Net margin",
              width: 104,
              align: "right",
              render: (r) => <span style={{ color: marginColor(r.net_margin_usd) }}>{fmtUsd(r.net_margin_usd)}</span>,
            },
            {
              header: "Conf.",
              width: 72,
              align: "right",
              render: (r) => <span style={{ color: "var(--text-faint)" }}>{fmtScore(r.confidence)}</span>,
            },
          ]}
        />
      ) : (
        <Empty>No valued capabilities yet. Registry records remain available under Capabilities.</Empty>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

function CapabilityRankingPanel({ load }: { load: Loadable<CapabilityRankingRow[]> }) {
  return (
    <ConsoleSection title="Capability ranking" className={styles.panel}>
      <ConsoleSurface>
      {load.loading && !load.data ? (
        <SkeletonRows n={6} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <MiniTable<CapabilityRankingRow>
          ariaLabel="Capability ranking"
          evidenceId="regulus.table.capability-ranking"
          minWidth={560}
          rows={[...load.data].sort((a, b) => b.net_margin_usd - a.net_margin_usd).slice(0, 20)}
          rowKey={(r) => r.capability_id}
          cols={[
            {
              header: "Capability",
              render: (r) => <Trunc title={r.capability_id}>{r.capability_id}</Trunc>,
            },
            {
              header: "Type",
              width: 128,
              render: (r) => <Pill tone={capTypeTone(r.capability_type)}>{r.capability_type}</Pill>,
            },
            {
              header: "Net margin",
              width: 104,
              align: "right",
              render: (r) => <span style={{ color: marginColor(r.net_margin_usd) }}>{fmtUsd(r.net_margin_usd)}</span>,
            },
            { header: "AER", width: 76, align: "right", render: (r) => fmtNum(r.aer) },
            {
              header: "Protected",
              width: 96,
              align: "right",
              render: (r) =>
                r.is_protected ? (
                  <Pill tone="accent">Protected</Pill>
                ) : (
                  <span style={{ color: "var(--text-faint)" }}>—</span>
                ),
            },
          ]}
        />
      ) : (
        <Empty>No valued capabilities are rankable yet. Record an outcome valuation first.</Empty>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

// --------------------------------------------------------------------------
// Confidence gate + data-quality mix
// --------------------------------------------------------------------------

function ConfidenceGatePanel({ load }: { load: Loadable<ConfidenceGateStatus> }) {
  return (
    <ConsoleSection title="Confidence gate" className={styles.panel}>
      <ConsoleSurface>
      {load.loading && !load.data ? (
        <SkeletonRows n={3} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.passed + load.data.blocked > 0 ? (
        <GateBody g={load.data} />
      ) : (
        <Empty>No confidence-gate evaluations yet.</Empty>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

function GateBody({ g }: { g: ConfidenceGateStatus }) {
  const total = g.passed + g.blocked;
  return (
    <div>
      <div className={styles.gateStats}>
        <Stat label="Passed" value={g.passed.toLocaleString()} tone="var(--success)" />
        <Stat label="Blocked" value={g.blocked.toLocaleString()} tone="var(--danger)" />
      </div>
      <div className={styles.gateChart}>
        <StackedBar
          segments={[
            { value: g.passed, color: "var(--success)" },
            { value: g.blocked, color: "var(--danger)" },
          ]}
        />
      </div>
      <div className={styles.gateMeta}>
        {fmtPct(g.passed / total)} passed of {total.toLocaleString()} evaluated
      </div>
    </div>
  );
}

function DataQualityPanel({ load }: { load: Loadable<DataQualityMix> }) {
  const total = load.data ? load.data.measured + load.data.inferred + load.data.mixed : 0;
  return (
    <ConsoleSection title="Data-quality mix" className={styles.panel}>
      <ConsoleSurface>
      {load.loading && !load.data ? (
        <SkeletonRows n={3} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && total > 0 ? (
        <DqBody m={load.data} total={total} />
      ) : (
        <Empty>No outcome provenance recorded yet.</Empty>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

function DqBody({ m, total }: { m: DataQualityMix; total: number }) {
  return (
    <div>
      <StackedBar
        segments={[
          { value: m.measured, color: "var(--success)" },
          { value: m.inferred, color: "var(--warning)" },
          { value: m.mixed, color: "var(--info)" },
        ]}
      />
      <div className={styles.legend}>
        <Legend color="var(--success)" label="Measured" value={m.measured} total={total} />
        <Legend color="var(--warning)" label="Inferred" value={m.inferred} total={total} />
        <Legend color="var(--info)" label="Mixed" value={m.mixed} total={total} />
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Policy timeline
// --------------------------------------------------------------------------

function PolicyTimelinePanel({ load }: { load: Loadable<PolicyTimelineRow[]> }) {
  return (
    <ConsoleSection title="Policy timeline" className={styles.panel}>
      <ConsoleSurface>
      {load.loading && !load.data ? (
        <SkeletonRows n={5} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <MiniTable<PolicyTimelineRow>
          ariaLabel="Policy timeline"
          evidenceId="regulus.table.policy-timeline"
          minWidth={640}
          rows={[...load.data]
            .sort((a, b) => new Date(b.proposed_at).getTime() - new Date(a.proposed_at).getTime())
            .slice(0, 20)}
          rowKey={(r) => r.id}
          cols={[
            {
              header: "Capability",
              render: (r) => <Trunc title={r.capability_id}>{r.capability_id}</Trunc>,
            },
            {
              header: "Action",
              width: 150,
              render: (r) => <Trunc title={r.action_type}>{r.action_type}</Trunc>,
            },
            {
              header: "Status",
              width: 104,
              render: (r) => <Pill tone={policyTone(r.status)}>{r.status}</Pill>,
            },
            { header: "Proposed", width: 112, render: (r) => fmtDate(r.proposed_at) },
            {
              header: "Approved",
              width: 112,
              render: (r) => <span style={{ color: "var(--text-faint)" }}>{fmtDate(r.approved_at)}</span>,
            },
            {
              header: "Applied",
              width: 112,
              render: (r) => <span style={{ color: "var(--text-faint)" }}>{fmtDate(r.applied_at)}</span>,
            },
          ]}
        />
      ) : (
        <Empty>No policy actions yet.</Empty>
      )}
      </ConsoleSurface>
    </ConsoleSection>
  );
}

// --------------------------------------------------------------------------
// Small shared pieces
// --------------------------------------------------------------------------

type Col<T> = {
  header: string;
  width?: number | string;
  align?: "left" | "right" | "center";
  render: (row: T) => ReactNode;
};

function MiniTable<T>({
  cols,
  rows,
  rowKey,
  minWidth,
  ariaLabel,
  evidenceId,
}: {
  cols: Col<T>[];
  rows: T[];
  rowKey: (row: T, i: number) => string | number;
  minWidth?: number;
  ariaLabel: string;
  evidenceId: string;
}) {
  return (
    <div
      className={styles.tableScroll}
      role="region"
      aria-label={ariaLabel}
      data-evidence-id={evidenceId}
      tabIndex={0}
    >
      <table className={styles.table} style={{ minWidth }}>
        <thead>
          <tr>
            {cols.map((c, i) => (
              <th key={i} style={{ width: c.width, textAlign: c.align ?? "left" }}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={rowKey(r, ri)}>
              {cols.map((c, ci) => (
                <td key={ci} style={{ textAlign: c.align ?? "left" }}>
                  {c.render(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Trunc({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span title={title} className={styles.truncate}>
      {children}
    </span>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className={styles.statValue} style={{ color: tone }}>
        {value}
      </div>
      <div className={styles.statLabel}>
        {label}
      </div>
    </div>
  );
}

function StackedBar({ segments }: { segments: { value: number; color: string }[] }) {
  const total = segments.reduce((a, s) => a + s.value, 0);
  return (
    <div className={styles.bar}>
      {total > 0
        ? segments.map((s, i) =>
            s.value > 0 ? <div key={i} style={{ width: `${(s.value / total) * 100}%`, background: s.color }} /> : null,
          )
        : null}
    </div>
  );
}

function Legend({ color, label, value, total }: { color: string; label: string; value: number; total: number }) {
  return (
    <span className={styles.legendItem}>
      <StatusDot tone={color} />
      <span>{label}</span>
      <span className={styles.legendValue}>
        {value.toLocaleString()} · {fmtPct(total > 0 ? value / total : 0)}
      </span>
    </span>
  );
}

function SkeletonRows({ n }: { n: number }) {
  return (
    <div className={styles.skeletonRows}>
      {Array.from({ length: n }).map((_, i) => (
        <Skeleton key={i} height={16} />
      ))}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className={styles.empty}>{children}</p>;
}

function ConnectNote() {
  return (
    <ConsoleNotice title="Not connected">
      Open Connect from the navigation to set the API base and key.
    </ConsoleNotice>
  );
}

function EconAbsentNote() {
  return (
    <ConsoleNotice title="Economics unavailable">
      The Regulus mount is disabled or this API key lacks the platform-admin role. Global metrics appear when an
      admin-scoped key reaches a Regulus-enabled deployment.
    </ConsoleNotice>
  );
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <ConsoleNotice
      tone="danger"
      title="Economics data unavailable"
      actions={<Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {message}
    </ConsoleNotice>
  );
}
