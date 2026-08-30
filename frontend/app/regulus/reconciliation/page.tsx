"use client";

// Regulus — Reconciliation screen.
//
// Reconciliation is how the econ plane keeps itself honest: measured ground-truth
// costs get imported, then every cost/value ESTIMATE is scored against them. This
// screen is the read-only view over that scoring, built on the P0 primitives in
// the P1/P2 house style (inline styles + CSS-var tokens, dark-only, useLoad + the
// mounted/connected gate, section heads, inline error+Retry). Nothing here crashes
// when the API is unconfigured, unreachable, or freshly seeded.
//
// TWO SURFACES (types are verbatim from the Regulus OpenAPI — see api-types.regulus.ts):
//
//   CalibrationSummary (rgCalibrationSummary) — estimated-vs-ground-truth accuracy:
//     capability_id | mape | mae | rmse | interval_coverage | bias | sample_size
//   TrendPoint[] (rgCalibrationTrend) — calibration error per reporting period: { x, y }.
//
// SHAPE NOTE: the generated OpenAPI types this endpoint's 200 body as
// CalibrationSummary[] (one row per capability), but the hand-written
// rgCalibrationSummary() helper is annotated to return a single object. Rather than
// trust either, toRows() normalizes whatever comes back (array OR lone object OR
// null) into a CalibrationSummary[]. A fresh econ DB returns [] — that is the normal
// empty state, NOT an error.
//
// UNITS: mae / rmse / bias are USD cost errors (ground truth is amount_usd, estimates
// are *_usd). mape and interval_coverage are dimensionless (a %-error and an interval
// hit-rate) — the console does NOT know their exact scale (ratio vs. percent), so it
// renders the raw wire value and captions the unit rather than inventing a transform.
// Only real wire fields are rendered; the full payload is always in the CodeBlock.
//
// Authentication uses the short-lived HttpOnly session cookie; the exchanged
// API key is never persisted, logged, or placed in a URL.

import { useEffect, useMemo, useState } from "react";
import {
  Button,
  CodeBlock,
  ConsoleEmpty,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  MonoLabel,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { useLoad } from "@/app/hooks/useLoad";
import { EconomicsWorkspaceNav } from "@/app/components/EconomicsWorkspaceNav";
import {
  rgCalibrationSummary,
  rgCalibrationTrend,
  type CalibrationSummary,
  type TrendPoint,
} from "@/app/lib/regulusApi";
import { isConfigured } from "@/app/lib/config";
import styles from "../subpages.module.css";

// --------------------------------------------------------------------------
// Formatting helpers — defensive about non-finite values so a tile never
// renders "NaN"/"Infinity".
// --------------------------------------------------------------------------

/** General number: grouping separators, ≤4 fractional digits, exponential
 *  fallback for the very tiny / very large so a tile never overflows. */
function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs !== 0 && (abs < 1e-4 || abs >= 1e12)) return n.toExponential(2);
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

/** Integer with grouping (sample counts). */
function fmtInt(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

/** Signed number — bias carries direction (over- vs under-estimation), so the
 *  sign is real information worth surfacing. */
function fmtSigned(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return fmtNum(0);
  return `${n > 0 ? "+" : "-"}${fmtNum(Math.abs(n))}`;
}

/** Normalize the calibration-summary body into a list regardless of whether the
 *  wire returned an array (per the generated OpenAPI), a lone object (per the
 *  helper's annotation), or null. */
function toRows(data: CalibrationSummary[] | CalibrationSummary | null): CalibrationSummary[] {
  if (data == null) return [];
  const raw: unknown = data;
  if (Array.isArray(raw)) return raw.filter((r): r is CalibrationSummary => r != null);
  return [raw as CalibrationSummary];
}

/** Cap how many per-capability tile groups render — the full list is always in
 *  the CodeBlock fallback below. */
const MAX_GROUPS = 8;

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function ReconciliationPage() {
  // Config lives in localStorage; read it only after mount so the static
  // prerender and the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Reconciliation"
        description="How well cost and value estimates match measured ground truth."
      />

      <EconomicsWorkspaceNav active="reconciliation" />

      <ConsoleNotice title="Data context">
        Scope: platform-admin control plane · Window: imported ground-truth history · Source: Regulus
        reconciliation · Freshness: current request. Restricted data returns an authorization explanation.
      </ConsoleNotice>

      <p className={styles.sectionNote}>
        Reconciliation compares imported measured costs with prior estimates. Without
        ground-truth imports, these metrics remain empty and calibration does not change.
      </p>

      <div className={styles.stack}>
        <CalibrationSummarySection connected={connected} mounted={mounted} />
        <CalibrationTrendSection connected={connected} mounted={mounted} />
      </div>
    </ConsolePage>
  );
}

// --------------------------------------------------------------------------
// Calibration summary — per-capability accuracy tiles + raw JSON fallback.
// --------------------------------------------------------------------------

function CalibrationSummarySection({
  connected,
  mounted,
}: {
  connected: boolean;
  mounted: boolean;
}) {
  const summary = useLoad<CalibrationSummary[]>(rgCalibrationSummary);
  const rows = useMemo(() => toRows(summary.data), [summary.data]);
  const rawText = useMemo(() => {
    if (summary.data == null) return "";
    try {
      return JSON.stringify(summary.data, null, 2) ?? String(summary.data);
    } catch {
      return String(summary.data);
    }
  }, [summary.data]);

  const totalSamples = rows.reduce(
    (acc, r) => acc + (Number.isFinite(r.sample_size) ? r.sample_size : 0),
    0,
  );
  const shown = rows.slice(0, MAX_GROUPS);
  const overflow = rows.length - shown.length;

  return (
    <ConsoleSection
      title="Calibration summary"
      evidenceScope="calibration-summary"
      actions={connected ? (
          <Button
            onClick={summary.reload}
            disabled={summary.loading}
          >
            {summary.loading ? "Loading…" : "Refresh"}
          </Button>
        ) : undefined}
    >

      <p className={styles.sectionNote}>
        Estimated-vs-ground-truth error, one row per calibrated capability — mean
        absolute error (MAE), root-mean-square error (RMSE), mean absolute percentage
        error (MAPE), how often the credible interval covered the truth, and the
        signed bias.
      </p>

      {!mounted ? (
        <SummarySkeleton />
      ) : !connected ? (
        <ConsoleNotice title="Not connected">
          Open Connect in the sidebar to load calibration.
        </ConsoleNotice>
      ) : summary.loading && !summary.data ? (
        <SummarySkeleton />
      ) : summary.error ? (
        <InlineError message={summary.error} onRetry={summary.reload} />
      ) : rows.length === 0 ? (
        <ConsoleEmpty>
          No calibration data yet — import ground truth to score estimates.
        </ConsoleEmpty>
      ) : (
        <div className={styles.panelStack}>
          <p className={styles.recordMeta}>
            {rows.length} {rows.length === 1 ? "capability" : "capabilities"} ·{" "}
            {fmtInt(totalSamples)} {totalSamples === 1 ? "sample" : "samples"} reconciled
          </p>

          {shown.map((r, i) => (
            <CapabilityCalibration key={`${r.capability_id}-${i}`} row={r} />
          ))}

          {overflow > 0 && (
            <p className={styles.overflowNote}>
              +{overflow} more {overflow === 1 ? "capability" : "capabilities"} in the
              payload below.
            </p>
          )}

          <CodeBlock label="Calibration summary (JSON)" code={rawText} />
        </div>
      )}
    </ConsoleSection>
  );
}

function SummarySkeleton() {
  return (
    <ConsoleSurface>
      <Skeleton width={220} height={12} />
      <div style={{ height: 14 }} />
      <div className={styles.metricGrid}>
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} height={62} />
        ))}
      </div>
    </ConsoleSurface>
  );
}

/** One capability's calibration: a labeled header + the six metric tiles. */
function CapabilityCalibration({ row: r }: { row: CalibrationSummary }) {
  const hasSamples = Number.isFinite(r.sample_size) && r.sample_size > 0;

  const tiles: {
    key: string;
    label: string;
    value: string;
    caption: string;
    tone?: string;
  }[] = [
    {
      key: "sample_size",
      label: "Samples",
      value: fmtInt(r.sample_size),
      caption: "count",
      tone: hasSamples ? "accent" : "muted",
    },
    { key: "mape", label: "MAPE", value: fmtNum(r.mape), caption: "mean abs % err" },
    { key: "mae", label: "MAE", value: fmtNum(r.mae), caption: "USD abs err" },
    { key: "rmse", label: "RMSE", value: fmtNum(r.rmse), caption: "USD rms err" },
    {
      key: "interval_coverage",
      label: "Coverage",
      value: fmtNum(r.interval_coverage),
      caption: "interval hit-rate",
    },
    { key: "bias", label: "Bias", value: fmtSigned(r.bias), caption: "USD · signed" },
  ];

  return (
    <ConsoleSurface className={styles.resultStack}>
      <div className={styles.recordHeader}>
        <span
          title={r.capability_id}
          style={{
            minWidth: 0,
            fontFamily: "var(--font-mono)",
            fontSize: 12.5,
            fontWeight: 600,
            color: "var(--text-primary)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {r.capability_id || "—"}
        </span>
        <Pill tone={hasSamples ? "accent" : "muted"} style={{ flexShrink: 0 }}>
          <StatusDot tone={hasSamples ? "accent" : "muted"} />
          {hasSamples ? `n=${fmtInt(r.sample_size)}` : "no samples"}
        </Pill>
      </div>

      <div className={styles.metricGrid}>
        {tiles.map((t) => (
          <StatTile
            key={t.key}
            label={t.label}
            value={t.value}
            caption={t.caption}
            tone={t.tone}
          />
        ))}
      </div>
    </ConsoleSurface>
  );
}

// --------------------------------------------------------------------------
// Calibration trend — a compact inline SVG sparkline (no charting library).
// --------------------------------------------------------------------------

function CalibrationTrendSection({
  connected,
  mounted,
}: {
  connected: boolean;
  mounted: boolean;
}) {
  const trend = useLoad<TrendPoint[]>(rgCalibrationTrend);
  // Keep only finite-y points so the sparkline geometry can never go NaN.
  const points = useMemo(
    () => (trend.data ?? []).filter((p) => Number.isFinite(p.y)),
    [trend.data],
  );
  const latest = points.length > 0 ? points[points.length - 1] : null;

  return (
    <ConsoleSection
      title="Calibration trend"
      evidenceScope="calibration-trend"
      actions={connected ? (
          <Button
            onClick={trend.reload}
            disabled={trend.loading}
          >
            {trend.loading ? "Loading…" : "Refresh"}
          </Button>
        ) : undefined}
    >

      <p className={styles.sectionNote}>
        Calibration error across reporting periods — lower means estimates track
        measured ground truth more closely.
      </p>

      {!mounted ? (
        <ConsoleSurface>
          <Skeleton width={140} height={36} />
        </ConsoleSurface>
      ) : !connected ? (
        <ConsoleNotice title="Not connected">
          Open Connect in the sidebar to load the trend.
        </ConsoleNotice>
      ) : trend.loading && !trend.data ? (
        <ConsoleSurface>
          <Skeleton width={140} height={36} />
        </ConsoleSurface>
      ) : trend.error ? (
        <InlineError message={trend.error} onRetry={trend.reload} />
      ) : points.length === 0 ? (
        <ConsoleEmpty>No calibration history yet.</ConsoleEmpty>
      ) : (
        <ConsoleSurface>
          <div className={styles.trendSummary}>
            <Sparkline points={points} />
            <div className={styles.trendLatest}>
              <MonoLabel style={{ color: "var(--text-faint)" }}>Latest</MonoLabel>
              <div className={styles.trendValue}>
                {latest ? fmtNum(latest.y) : "—"}
              </div>
              {latest && (
                <div
                  title={latest.x}
                  className={styles.trendMeta}
                >
                  {latest.x} · {points.length}{" "}
                  {points.length === 1 ? "period" : "periods"}
                </div>
              )}
            </div>
          </div>
        </ConsoleSurface>
      )}
    </ConsoleSection>
  );
}

/** Inline sparkline: a polyline over the series with an end-point marker.
 *  Y is normalized min→max (higher value sits higher on the chart); X is the
 *  point index. A flat series (min == max) draws a horizontal mid-line; a single
 *  point renders just the centered end marker. Stroke is var(--accent). */
function Sparkline({
  points,
  width = 140,
  height = 36,
}: {
  points: TrendPoint[];
  width?: number;
  height?: number;
}) {
  const n = points.length;
  if (n === 0) return null;

  const pad = 4;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  const ys = points.map((p) => p.y);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min;

  const coords = points.map((p, i) => {
    const x = n === 1 ? width / 2 : pad + (i / (n - 1)) * innerW;
    const y = span === 0 ? pad + innerH / 2 : pad + innerH - ((p.y - min) / span) * innerH;
    return { x, y };
  });
  const line = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const last = coords[coords.length - 1];

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`Calibration trend, ${n} ${n === 1 ? "point" : "points"}`}
      style={{ display: "block", flexShrink: 0 }}
    >
      {n > 1 && (
        <polyline
          points={line}
          fill="none"
          stroke="var(--accent)"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
      <circle cx={last.x} cy={last.y} r={2.2} fill="var(--accent)" />
    </svg>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirror the Metrics / Connectors screen conventions).
// --------------------------------------------------------------------------

function StatTile({
  label,
  value,
  caption,
  tone,
}: {
  label: string;
  value: string;
  caption?: string;
  tone?: string;
}) {
  return (
    <div className={styles.metricTile}>
      <div
        title={value}
        className={styles.metricValue}
        style={{
          color: tone ? (TONE_VAR[tone] ?? "var(--text-primary)") : "var(--text-primary)",
        }}
      >
        {value}
      </div>
      <div
        title={label}
        className={styles.metricLabel}
      >
        {label}
      </div>
      {caption && (
        <div className={styles.metricCaption}>
          {caption}
        </div>
      )}
    </div>
  );
}

/** Small tone→CSS-var map for the one tile (Samples) that gets emphasis. */
const TONE_VAR: Record<string, string> = {
  accent: "var(--accent)",
  muted: "var(--text-faint)",
};

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <p className={styles.emptyInline}>{children}</p>;
}

function InlineError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  const restricted = message.startsWith("403");
  return (
    <ConsoleNotice
      tone={restricted ? undefined : "danger"}
      title={restricted ? "Access restricted" : "Reconciliation data unavailable"}
      actions={restricted ? undefined : <Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {restricted
        ? "This API key cannot read platform reconciliation data. Connect with a metrics-read or platform-admin credential."
        : message}
    </ConsoleNotice>
  );
}
