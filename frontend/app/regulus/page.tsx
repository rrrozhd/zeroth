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
import type { CSSProperties, ReactNode } from "react";
import { Button, Card, Pill, Skeleton, StatusDot } from "@/app/components/primitives";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import { useRegulus } from "@/app/components/regulusContext";
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

const MONO = "var(--font-mono)";
const TRACK = "#1a1f29"; // bar-track color shared with the Cost screen

// --------------------------------------------------------------------------
// Formatters (all client-side — panels only render after a client fetch, so
// locale formatting can't cause a hydration mismatch).
// --------------------------------------------------------------------------

function fmtUsd(n: number): string {
  if (!Number.isFinite(n)) return "$0.00";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

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
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 22 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Econ Dashboard</h1>
            <Pill tone="accent">Global</Pill>
          </div>
          <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
            Platform economic control plane — global across tenants.
          </p>
        </div>
        {connected && (
          <Button variant="neutral" onClick={reloadAll} disabled={anyLoading} style={{ flexShrink: 0 }}>
            {anyLoading ? "Refreshing…" : "Refresh"}
          </Button>
        )}
      </header>

      {!connected ? (
        <ConnectNote />
      ) : reg === "absent" ? (
        <EconAbsentNote />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <KpiRow load={kpis} />

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
              gap: 12,
            }}
          >
            <TrendPanel label="Confidence" load={confTrend} />
            <TrendPanel label="Efficiency" load={effTrend} />
            <TrendPanel label="Calibration" load={calibTrend} />
            <TrendPanel label="Action suppression" load={suppTrend} />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0,1fr))", gap: 12 }}>
            <ValueRankPanel label="Top creators" load={topCreators} order="desc" />
            <ValueRankPanel label="Capital destroyers" load={destroyers} order="asc" />
          </div>

          <CapabilityRankingPanel load={ranking} />

          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0,1fr))", gap: 12 }}>
            <ConfidenceGatePanel load={gate} />
            <DataQualityPanel load={dqMix} />
          </div>

          <PolicyTimelinePanel load={policy} />
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// KPI row
// --------------------------------------------------------------------------

function KpiRow({ load }: { load: Loadable<KPIResponse> }) {
  const gridStyle: CSSProperties = {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
    gap: 12,
  };

  if (load.loading && !load.data) {
    return (
      <div style={gridStyle}>
        {Array.from({ length: 5 }).map((_, i) => (
          <Card key={i} pad={16} style={{ minWidth: 0 }}>
            <Skeleton height={11} width={90} />
            <div style={{ marginTop: 12 }}>
              <Skeleton height={26} width={120} />
            </div>
          </Card>
        ))}
      </div>
    );
  }
  if (load.error && !load.data) {
    return (
      <Card pad={16}>
        <InlineError message={load.error} onRetry={load.reload} />
      </Card>
    );
  }
  const k = load.data;
  if (!k) {
    return (
      <Card pad={16}>
        <Empty>No KPIs yet.</Empty>
      </Card>
    );
  }

  const tiles: { label: string; value: string; tone?: string }[] = [
    { label: "Total AI spend", value: fmtUsd(k.total_ai_spend_usd) },
    { label: "Total AI value", value: fmtUsd(k.total_ai_value_usd) },
    { label: "Net AI margin", value: fmtUsd(k.net_ai_margin_usd), tone: marginColor(k.net_ai_margin_usd) },
    { label: "Portfolio confidence", value: fmtScore(k.portfolio_confidence_score) },
    { label: "Efficiency index", value: fmtNum(k.efficiency_index) },
  ];

  return (
    <div style={gridStyle}>
      {tiles.map((t) => (
        <Card key={t.label} label={t.label} pad={16} style={{ minWidth: 0 }}>
          <div
            style={{
              fontFamily: MONO,
              fontSize: 26,
              fontWeight: 600,
              lineHeight: 1.1,
              color: t.tone ?? "var(--text-primary)",
              overflowWrap: "anywhere",
            }}
          >
            {t.value}
          </div>
        </Card>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------------
// Trends — inline SVG sparklines
// --------------------------------------------------------------------------

function TrendPanel({ label, load }: { label: string; load: Loadable<TrendPoint[]> }) {
  return (
    <Card label={label} pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <div>
          <Skeleton height={20} width={70} />
          <div style={{ marginTop: 12 }}>
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
    </Card>
  );
}

function TrendBody({ points }: { points: TrendPoint[] }) {
  const latest = points[points.length - 1];
  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontFamily: MONO, fontSize: 20, fontWeight: 600, lineHeight: 1.1, color: "var(--text-primary)" }}>
          {fmtNum(latest.y)}
        </span>
        <span style={{ fontFamily: MONO, fontSize: 10.5, color: "var(--text-faint)", whiteSpace: "nowrap" }}>
          {shortX(latest.x)}
        </span>
      </div>
      <div style={{ marginTop: 10 }}>
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
    <Card label={label} pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <SkeletonRows n={5} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <MiniTable<CapabilityValueRow>
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
        <Empty>No capabilities yet.</Empty>
      )}
    </Card>
  );
}

function CapabilityRankingPanel({ load }: { load: Loadable<CapabilityRankingRow[]> }) {
  return (
    <Card label="Capability ranking" pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <SkeletonRows n={6} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <MiniTable<CapabilityRankingRow>
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
        <Empty>No ranked capabilities yet.</Empty>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Confidence gate + data-quality mix
// --------------------------------------------------------------------------

function ConfidenceGatePanel({ load }: { load: Loadable<ConfidenceGateStatus> }) {
  return (
    <Card label="Confidence gate" pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <SkeletonRows n={3} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.passed + load.data.blocked > 0 ? (
        <GateBody g={load.data} />
      ) : (
        <Empty>No confidence-gate evaluations yet.</Empty>
      )}
    </Card>
  );
}

function GateBody({ g }: { g: ConfidenceGateStatus }) {
  const total = g.passed + g.blocked;
  return (
    <div>
      <div style={{ display: "flex", gap: 28 }}>
        <Stat label="Passed" value={g.passed.toLocaleString()} tone="var(--success)" />
        <Stat label="Blocked" value={g.blocked.toLocaleString()} tone="var(--danger)" />
      </div>
      <div style={{ marginTop: 14 }}>
        <StackedBar
          segments={[
            { value: g.passed, color: "var(--success)" },
            { value: g.blocked, color: "var(--danger)" },
          ]}
        />
      </div>
      <div style={{ marginTop: 8, fontFamily: MONO, fontSize: 11, color: "var(--text-faint)" }}>
        {fmtPct(g.passed / total)} passed of {total.toLocaleString()} evaluated
      </div>
    </div>
  );
}

function DataQualityPanel({ load }: { load: Loadable<DataQualityMix> }) {
  const total = load.data ? load.data.measured + load.data.inferred + load.data.mixed : 0;
  return (
    <Card label="Data-quality mix" pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <SkeletonRows n={3} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && total > 0 ? (
        <DqBody m={load.data} total={total} />
      ) : (
        <Empty>No outcome provenance recorded yet.</Empty>
      )}
    </Card>
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
      <div style={{ marginTop: 12, display: "flex", flexWrap: "wrap", gap: "8px 18px" }}>
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
    <Card label="Policy timeline" pad={16} style={{ minWidth: 0 }}>
      {load.loading && !load.data ? (
        <SkeletonRows n={5} />
      ) : load.error && !load.data ? (
        <InlineError message={load.error} onRetry={load.reload} />
      ) : load.data && load.data.length > 0 ? (
        <MiniTable<PolicyTimelineRow>
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
    </Card>
  );
}

// --------------------------------------------------------------------------
// Small shared pieces
// --------------------------------------------------------------------------

const TH_STYLE: CSSProperties = {
  fontFamily: MONO,
  fontSize: 10,
  fontWeight: 500,
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  color: "var(--text-faint)",
  padding: "0 8px 8px 0",
  whiteSpace: "nowrap",
};

const TD_STYLE: CSSProperties = {
  fontFamily: MONO,
  fontSize: 12,
  color: "var(--text-secondary)",
  padding: "8px 8px 8px 0",
  borderTop: "1px solid var(--hair)",
  overflow: "hidden",
};

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
}: {
  cols: Col<T>[];
  rows: T[];
  rowKey: (row: T, i: number) => string | number;
  minWidth?: number;
}) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", minWidth, borderCollapse: "collapse", tableLayout: "fixed" }}>
        <thead>
          <tr>
            {cols.map((c, i) => (
              <th key={i} style={{ ...TH_STYLE, width: c.width, textAlign: c.align ?? "left" }}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={rowKey(r, ri)}>
              {cols.map((c, ci) => (
                <td key={ci} style={{ ...TD_STYLE, textAlign: c.align ?? "left" }}>
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
    <span
      title={title}
      style={{
        display: "block",
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        color: "var(--text-primary)",
      }}
    >
      {children}
    </span>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div style={{ fontFamily: MONO, fontSize: 22, fontWeight: 600, lineHeight: 1.1, color: tone ?? "var(--text-primary)" }}>
        {value}
      </div>
      <div
        style={{
          marginTop: 4,
          fontFamily: MONO,
          fontSize: 10,
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          color: "var(--text-faint)",
        }}
      >
        {label}
      </div>
    </div>
  );
}

function StackedBar({ segments }: { segments: { value: number; color: string }[] }) {
  const total = segments.reduce((a, s) => a + s.value, 0);
  return (
    <div style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden", background: TRACK }}>
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
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <StatusDot tone={color} />
      <span style={{ fontFamily: MONO, fontSize: 11, color: "var(--text-secondary)" }}>{label}</span>
      <span style={{ fontFamily: MONO, fontSize: 11, color: "var(--text-faint)" }}>
        {value.toLocaleString()} · {fmtPct(total > 0 ? value / total : 0)}
      </span>
    </span>
  );
}

function SkeletonRows({ n }: { n: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {Array.from({ length: n }).map((_, i) => (
        <Skeleton key={i} height={16} />
      ))}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div style={{ fontSize: 12.5, color: "var(--text-faint)", lineHeight: 1.55 }}>{children}</div>;
}

function ConnectNote() {
  return (
    <Card pad={20}>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Not connected. Open <span style={{ color: "var(--accent)" }}>Connect</span> (bottom-left) to set the API base and
        key.
      </div>
    </Card>
  );
}

function EconAbsentNote() {
  return (
    <Card pad={20}>
      <div style={{ fontSize: 13, color: "var(--text-muted)", lineHeight: 1.55 }}>
        The econ control plane is not available for this connection — the Regulus mount is disabled, or the API key lacks
        the platform-admin role. Global econ metrics appear here once an admin-scoped key reaches a Regulus-enabled
        deployment.
      </div>
    </Card>
  );
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
