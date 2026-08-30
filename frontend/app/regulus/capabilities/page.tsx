"use client";

// The Regulus Capabilities registry — a master-detail operator view over the
// econ control plane's capability registry, reached only through the admin-gated
// Regulus proxy (see lib/regulusApi). It is the "all capabilities" surface: the
// registered capabilities on the left, and for the selected one its registry
// fields, per-implementation value comparison, a drift sparkline, and the raw
// evaluation payloads on the right.
//
// Left: every registered capability (rgCapabilities). Right: the selected
// capability's detail (rgCapability) plus four independently-loaded panels.
//
// Each detail panel owns its own load/empty/error state via its own `useLoad`,
// so one panel coming back empty (a fresh econ DB has no evaluations/compares/
// drift yet) never blanks the others and never crashes the screen. A genuine
// error degrades to an inline Retry; a 404 or an empty array/object degrades to
// an empty note — a fresh registry is a normal, non-error state.
//
// The evaluation endpoints (latest/history) are typed `unknown` on the wire
// (the Regulus OpenAPI leaves them open), so they are rendered verbatim as
// pretty-printed JSON in a CodeBlock rather than being narrowed to a fabricated
// shape. Everything else renders only real, generated fields.
//
// Authentication uses the short-lived HttpOnly session cookie; the exchanged
// API key is never persisted, logged, or placed in a URL.

import { useEffect, useMemo, useState } from "react";
import {
  Button,
  CodeBlock,
  ConsoleNotice,
  MonoLabel,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { EconomicsPanel } from "@/app/regulus/EconomicsPanel";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import { ApiError, getIdentity } from "@/app/lib/api";
import { fmtUsd } from "@/app/components/ui";
import { isConfigured } from "@/app/lib/config";
import { regulusAccess, type RegulusAccess } from "@/app/regulus/regulus-access";
import {
  rgCapabilities,
  rgCapability,
  rgDriftTimeline,
  rgEvaluationsHistory,
  rgEvaluationsLatest,
  rgImplementationCompare,
  type CapabilityOut,
  type ImplementationCompareRow,
  type TrendPoint,
} from "@/app/lib/regulusApi";
import styles from "./capabilities.module.css";

// --------------------------------------------------------------------------
// Formatting + tone helpers
// --------------------------------------------------------------------------

/** Trim trailing zeros off a fixed-precision number (drift scores etc.). */
function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return String(n);
  return String(Number(n.toFixed(4)));
}

function fmtDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], { hour12: false });
}

/** A TrendPoint `x` may be an ISO timestamp or an opaque bucket label. */
function fmtX(x: string): string {
  const d = new Date(x);
  return Number.isNaN(d.getTime()) ? x : d.toLocaleString([], { hour12: false });
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Fresh-DB reads legitimately come back empty. Treat null / `[]` / `{}` as
 *  "nothing yet" rather than as content to render. */
function isEmptyPayload(v: unknown): boolean {
  if (v == null) return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === "object") return Object.keys(v as object).length === 0;
  return false;
}

/** Swallow a 404 into an empty fallback so a not-yet-populated sub-resource on a
 *  fresh econ DB shows an empty state, not a scary inline error. Any other
 *  failure rethrows and surfaces as a real error with a Retry. */
function emptyOn404<T>(fallback: T) {
  return (e: unknown): T => {
    if (e instanceof ApiError && e.status === 404) return fallback;
    throw e;
  };
}

const TYPE_TONE: Record<string, string> = {
  REVENUE: "success",
  COST: "warning",
  RISK: "danger",
  PRODUCTIVITY: "info",
};
function typeTone(t: string): string {
  return TYPE_TONE[(t ?? "").toUpperCase()] ?? "neutral";
}

const CRIT_TONE: Record<string, string> = {
  HIGH: "danger",
  MED: "warning",
  LOW: "muted",
};
function critTone(c: string): string {
  return CRIT_TONE[(c ?? "").toUpperCase()] ?? "neutral";
}

function marginTone(n: number): string {
  if (!Number.isFinite(n) || n === 0) return "neutral";
  return n > 0 ? "success" : "danger";
}

// --------------------------------------------------------------------------
// Page shell — master-detail (mirrors the Deployments screen conventions)
// --------------------------------------------------------------------------

export default function CapabilitiesPage() {
  // localStorage-derived config is read only after mount so the static prerender
  // and the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  if (!connected) return <CapabilitiesDisconnected mounted={mounted} />;
  return <CapabilitiesAccessBoundary />;
}

function CapabilitiesDisconnected({ mounted }: { mounted: boolean }) {
  const capabilities: Loadable<CapabilityOut[]> = {
    data: null,
    error: null,
    loading: !mounted,
    reload: () => undefined,
  };
  return (
    <div className={styles.workspace} data-evidence-id="regulus.capabilities.page">
      <ListPane
        capabilities={capabilities}
        connected={false}
        mounted={mounted}
        selectedId={null}
        onSelect={() => undefined}
      />
      <div className={styles.detailPane}><DetailPlaceholder /></div>
    </div>
  );
}

function CapabilitiesAccessBoundary() {
  const identity = useLoad(getIdentity);
  if (identity.loading && !identity.data) {
    return (
      <CapabilitiesAccessState title="Verifying access">
        Resolving role and tenant scope.
      </CapabilitiesAccessState>
    );
  }

  const access = regulusAccess(identity.data, identity.error);
  if (!access.canRead) {
    return (
      <CapabilitiesAccessState
        access={access}
        evidenceId="regulus.capabilities.access.restricted"
        title="Capabilities access restricted"
      >
        This role does not include metrics:read. Capability registry data is hidden for this
        credential and no protected read was issued.
      </CapabilitiesAccessState>
    );
  }
  return <CapabilitiesWorkspace access={access} />;
}

function CapabilitiesAccessState({
  access,
  evidenceId = "regulus.capabilities.access.resolving",
  title,
  children,
}: {
  access?: RegulusAccess;
  evidenceId?: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className={styles.workspace} data-evidence-id="regulus.capabilities.page">
      <aside aria-label="Capability list" className={styles.listPane}>
        <div className={styles.listHeader}><h1 className={styles.listTitle}>Capabilities</h1></div>
      </aside>
      <div className={styles.detailPane}>
        <div className={styles.detail}>
          {access?.scope && (
            <div data-evidence-id="regulus.capabilities.scope" className={styles.emptyNote}>
              Scope: {access.scope} · Role: {access.roles}
            </div>
          )}
          <div data-evidence-id={evidenceId}>
            <ConsoleNotice title={title}>{children}</ConsoleNotice>
          </div>
        </div>
      </div>
    </div>
  );
}

function CapabilitiesWorkspace({ access }: { access: RegulusAccess }) {
  const capabilities = useLoad<CapabilityOut[]>(rgCapabilities);

  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = capabilities.data ?? [];
  const selected = useMemo(
    () => list.find((c) => c.id === selectedId) ?? null,
    [list, selectedId],
  );

  return (
    <div
      className={styles.workspace}
      data-evidence-id="regulus.capabilities.registry"
      data-access-source={access.source}
    >
      <ListPane
        capabilities={capabilities}
        connected
        mounted
        selectedId={selectedId}
        onSelect={(c) => setSelectedId(c.id)}
      />
      <div className={styles.detailPane}>
        <div data-evidence-id="regulus.capabilities.scope" className={styles.emptyNote}>
          Scope: {access.scope} · Role: {access.roles}
        </div>
        {selected ? (
          <CapabilityDetail key={selected.id} capability={selected} />
        ) : (
          <DetailPlaceholder />
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Left list (~320px)
// --------------------------------------------------------------------------

function ListPane({
  capabilities,
  connected,
  mounted,
  selectedId,
  onSelect,
}: {
  capabilities: Loadable<CapabilityOut[]>;
  connected: boolean;
  mounted: boolean;
  selectedId: string | null;
  onSelect: (c: CapabilityOut) => void;
}) {
  const list = capabilities.data ?? [];
  return (
    <aside aria-label="Capability list" className={styles.listPane}>
      <div className={styles.listHeader}>
        <h1 className={styles.listTitle}>Capabilities</h1>
        {capabilities.data != null && (
          <span className={styles.listCount}>
            {list.length}
          </span>
        )}
      </div>

      <div className={styles.listBody}>
        {capabilities.loading && !capabilities.data ? (
          <div className={styles.loading}>
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} height={42} />
            ))}
          </div>
        ) : mounted && !connected ? (
          <EmptyNote>Connect to the API (top bar) to load capabilities.</EmptyNote>
        ) : capabilities.error ? (
          <div className={styles.emptyNote}>
            <InlineError message={capabilities.error} onRetry={capabilities.reload} />
          </div>
        ) : list.length === 0 ? (
          <EmptyNote>No capabilities registered.</EmptyNote>
        ) : (
          list.map((c) => (
            <CapabilityRow
              key={c.id}
              capability={c}
              selected={c.id === selectedId}
              onSelect={() => onSelect(c)}
            />
          ))
        )}
      </div>
    </aside>
  );
}

function CapabilityRow({
  capability: c,
  selected,
  onSelect,
}: {
  capability: CapabilityOut;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      data-evidence-id={`regulus.capabilities.capability.${c.id}`}
      className={`${styles.row} ${selected ? styles.rowSelected : ""}`}
    >
      <div className={styles.rowPrimary}>
        <StatusDot tone={critTone(c.criticality)} />
        <span
          className={styles.rowId}
          title={c.id}
        >
          {c.id}
        </span>
        <Pill tone={typeTone(c.type)} style={{ flexShrink: 0 }}>
          {c.type || "—"}
        </Pill>
      </div>
      <div className={styles.rowSecondary}>
        <span
          className={styles.rowName}
          title={c.name}
        >
          {c.name}
        </span>
        {c.is_protected && (
          <Pill tone="accent" style={{ flexShrink: 0 }}>
            protected
          </Pill>
        )}
      </div>
    </button>
  );
}

// --------------------------------------------------------------------------
// Detail
// --------------------------------------------------------------------------

function DetailPlaceholder() {
  return (
    <div className={styles.placeholder}>
      Select a capability to inspect.
    </div>
  );
}

function CapabilityDetail({ capability: c }: { capability: CapabilityOut }) {
  return (
    <div className={styles.detail}>
      {/* Header: name + id + type/criticality/protected */}
      <div className={styles.detailHeader}>
        <div className={styles.detailHeading}>
          <span className={styles.detailName}>{c.name}</span>
          <Pill tone={typeTone(c.type)}>{c.type || "—"}</Pill>
          <Pill tone={critTone(c.criticality)}>{c.criticality || "—"}</Pill>
          {c.is_protected && <Pill tone="accent">protected</Pill>}
        </div>
        <span className={styles.detailId}>
          {c.id}
        </span>
      </div>

      {/* Panels — each owns its own load/empty/error */}
      <OverviewPanel capabilityId={c.id} fallback={c} />
      <ComparePanel capabilityId={c.id} />
      <DriftPanel capabilityId={c.id} />
      <LatestEvaluationPanel capabilityId={c.id} />
      <EvaluationHistoryPanel capabilityId={c.id} />
    </div>
  );
}

// --------------------------------------------------------------------------
// Overview — the registry record (CapabilityOut) fetched fresh for the detail.
// --------------------------------------------------------------------------

function OverviewPanel({
  capabilityId,
  fallback,
}: {
  capabilityId: string;
  fallback: CapabilityOut;
}) {
  const cap = useLoad<CapabilityOut>(() => rgCapability(capabilityId));
  // Show the list row's copy until the authoritative fetch lands.
  const data = cap.data ?? fallback;

  return (
    <EconomicsPanel title="Overview" density="compact">
      {cap.error && !cap.data ? (
        <InlineError message={cap.error} onRetry={cap.reload} />
      ) : (
        <>
          <div className={styles.metaGrid}>
            <Meta label="id" value={data.id} />
            <Meta label="tenant" value={data.tenant_id} />
            <Meta label="type" value={data.type || "—"} />
            <Meta label="criticality" value={data.criticality || "—"} />
            <Meta label="protected" value={data.is_protected ? "yes" : "no"} />
            <Meta label="created" value={fmtDateTime(data.created_at)} />
          </div>

          {data.description ? (
            <p className={styles.description}>
              {data.description}
            </p>
          ) : (
            <p className={styles.empty}>
              No description.
            </p>
          )}

          {/* Valuation config — typed fields surfaced, then the raw block (its
              proxy_parameters are an open object). */}
          <div className={styles.metaGrid}>
            <Meta label="valuation method" value={data.valuation_config.valuation_method} />
            <Meta label="proxy formula" value={data.valuation_config.proxy_formula_id} />
            <Meta label="baseline source" value={data.valuation_config.baseline_source} />
            <Meta
              label="cost profile"
              value={data.valuation_config.cost_profile_id != null ? String(data.valuation_config.cost_profile_id) : "—"}
            />
            {data.valuation_config.confidence_gate && (
              <>
                <Meta
                  label="min confidence"
                  value={fmtNum(data.valuation_config.confidence_gate.min_confidence_level)}
                />
                <Meta
                  label="max rel. width"
                  value={fmtNum(data.valuation_config.confidence_gate.max_relative_width)}
                />
              </>
            )}
          </div>
          <CodeBlock label="Valuation config" code={jsonText(data.valuation_config)} />
        </>
      )}
    </EconomicsPanel>
  );
}

// --------------------------------------------------------------------------
// Implementation compare — per-implementation net margin + credible interval.
// --------------------------------------------------------------------------

function ComparePanel({ capabilityId }: { capabilityId: string }) {
  const compare = useLoad<ImplementationCompareRow[]>(() =>
    rgImplementationCompare(capabilityId).catch(emptyOn404<ImplementationCompareRow[]>([])),
  );
  const rows = Array.isArray(compare.data) ? compare.data : [];
  const hasBreakdown = rows.some(
    (r) => r.confidence_breakdown && Object.keys(r.confidence_breakdown).length > 0,
  );

  return (
    <EconomicsPanel title="Implementation compare" density="compact">
      {compare.loading && !compare.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} height={24} />
          ))}
        </div>
      ) : compare.error ? (
        <InlineError message={compare.error} onRetry={compare.reload} />
      ) : rows.length === 0 ? (
        <EmptyInline>No implementation comparisons yet.</EmptyInline>
      ) : (
        <>
          <div
            className={styles.tableScroll}
            role="region"
            aria-label="Implementation comparison"
            data-evidence-id="regulus.capability.table.implementation-comparison"
            tabIndex={0}
          >
            <table className={styles.table}>
              <thead>
                <tr>
                  <Th>Implementation</Th>
                  <Th>Arm</Th>
                  <Th align="right">Net margin</Th>
                  <Th align="right">CI low</Th>
                  <Th align="right">CI high</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.implementation_id}::${r.arm ?? ""}::${i}`}>
                    <Td mono title={r.implementation_id}>
                      {r.implementation_id}
                    </Td>
                    <Td mono>{r.arm ?? "—"}</Td>
                    <Td align="right" mono color={`var(--${marginTone(r.net_margin_usd)})`}>
                      {fmtUsd(r.net_margin_usd)}
                    </Td>
                    <Td align="right" mono color="var(--text-faint)">
                      {fmtUsd(r.confidence_low_usd)}
                    </Td>
                    <Td align="right" mono color="var(--text-faint)">
                      {fmtUsd(r.confidence_high_usd)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {hasBreakdown && (
            <div style={{ marginTop: 12 }}>
              <CodeBlock label="Raw rows (incl. confidence_breakdown)" code={jsonText(rows)} />
            </div>
          )}
        </>
      )}
    </EconomicsPanel>
  );
}

function Th({ children, align = "left" }: { children: React.ReactNode; align?: "left" | "right" }) {
  return (
    <th
      style={{
        textAlign: align,
        padding: "6px 10px",
        borderBottom: "1px solid var(--hair-strong)",
        fontSize: 11,
        fontWeight: 500,
        color: "var(--text-muted)",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = "left",
  mono = false,
  color,
  title,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  mono?: boolean;
  color?: string;
  title?: string;
}) {
  return (
    <td
      title={title}
      style={{
        textAlign: align,
        padding: "7px 10px",
        borderBottom: "1px solid var(--hair)",
        fontFamily: mono ? "var(--font-mono)" : "inherit",
        fontSize: 12,
        color: color ?? "var(--text-secondary)",
        whiteSpace: "nowrap",
        maxWidth: 260,
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {children}
    </td>
  );
}

// --------------------------------------------------------------------------
// Drift timeline — a compact inline SVG sparkline (no charting library).
// --------------------------------------------------------------------------

function DriftPanel({ capabilityId }: { capabilityId: string }) {
  const drift = useLoad<TrendPoint[]>(() =>
    rgDriftTimeline(capabilityId).catch(emptyOn404<TrendPoint[]>([])),
  );
  const points = Array.isArray(drift.data) ? drift.data : [];

  return (
    <EconomicsPanel title="Drift timeline" density="compact">
      {drift.loading && !drift.data ? (
        <Skeleton height={48} />
      ) : drift.error ? (
        <InlineError message={drift.error} onRetry={drift.reload} />
      ) : points.length === 0 ? (
        <EmptyInline>No drift history yet.</EmptyInline>
      ) : (
        <Sparkline points={points} />
      )}
    </EconomicsPanel>
  );
}

function Sparkline({ points }: { points: TrendPoint[] }) {
  const ys = points.map((p) => p.y).filter((y) => Number.isFinite(y));
  if (ys.length === 0) return <EmptyInline>No numeric drift points.</EmptyInline>;

  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min || 1;
  const n = points.length;
  const W = 100;
  const H = 30;
  const padY = 2;

  const coords = points.map((p, i) => {
    const x = n === 1 ? W / 2 : (i / (n - 1)) * W;
    const y = H - padY - ((p.y - min) / span) * (H - padY * 2);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const last = points[points.length - 1];
  const first = points[0];

  return (
    <div>
      {n === 1 ? (
        <div style={{ fontFamily: "var(--font-sans)", fontVariantNumeric: "tabular-nums", fontSize: 22, fontWeight: 500 }}>
          {fmtNum(last.y)}
        </div>
      ) : (
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          width="100%"
          height={48}
          role="img"
          aria-label="Drift score over time"
          style={{ display: "block" }}
        >
          <polyline
            points={coords.join(" ")}
            fill="none"
            stroke="var(--accent)"
            strokeWidth={1.25}
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
      )}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "4px 16px",
          marginTop: 8,
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-faint)",
        }}
      >
        <span>
          latest <b style={{ color: "var(--text-secondary)" }}>{fmtNum(last.y)}</b>
        </span>
        <span>min {fmtNum(min)}</span>
        <span>max {fmtNum(max)}</span>
        <span>{n} pts</span>
        <span style={{ marginLeft: "auto" }} title={`${first.x} → ${last.x}`}>
          {fmtX(first.x)} → {fmtX(last.x)}
        </span>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Latest evaluation — the wire type is `unknown`, so render verbatim as JSON.
// --------------------------------------------------------------------------

function LatestEvaluationPanel({ capabilityId }: { capabilityId: string }) {
  const latest = useLoad<unknown>(() =>
    rgEvaluationsLatest(capabilityId).catch(emptyOn404<unknown>(null)),
  );

  return (
    <EconomicsPanel title="Latest evaluation" density="compact">
      {latest.loading && !latest.data ? (
        <Skeleton height={90} />
      ) : latest.error ? (
        <InlineError message={latest.error} onRetry={latest.reload} />
      ) : isEmptyPayload(latest.data) ? (
        <EmptyInline>No evaluation runs yet.</EmptyInline>
      ) : (
        <CodeBlock code={jsonText(latest.data)} />
      )}
    </EconomicsPanel>
  );
}

// --------------------------------------------------------------------------
// Evaluation history — collapsible; also `unknown` on the wire → JSON verbatim.
// --------------------------------------------------------------------------

function EvaluationHistoryPanel({ capabilityId }: { capabilityId: string }) {
  const [open, setOpen] = useState(false);

  return (
    <EconomicsPanel title="Evaluation history" density="compact">
      <Button variant="neutral" onClick={() => setOpen((v) => !v)}>
        {open ? "Hide history" : "Show history"}
      </Button>
      {open && (
        <div className={styles.history}>
          <EvaluationHistoryBody capabilityId={capabilityId} />
        </div>
      )}
    </EconomicsPanel>
  );
}

function EvaluationHistoryBody({ capabilityId }: { capabilityId: string }) {
  const history = useLoad<unknown>(() =>
    rgEvaluationsHistory(capabilityId).catch(emptyOn404<unknown>(null)),
  );

  if (history.loading && !history.data) return <Skeleton height={120} />;
  if (history.error) return <InlineError message={history.error} onRetry={history.reload} />;
  if (isEmptyPayload(history.data)) return <EmptyInline>No evaluation history yet.</EmptyInline>;
  return <CodeBlock code={jsonText(history.data)} />;
}

// --------------------------------------------------------------------------
// Shared bits (mirrors the Deployments screen)
// --------------------------------------------------------------------------

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.meta}>
      <MonoLabel className={styles.metaLabel}>{label}</MonoLabel>
      <span className={styles.metaValue}>
        {value}
      </span>
    </div>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <p className={styles.emptyNote}>
      {children}
    </p>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <p className={styles.empty}>{children}</p>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <ConsoleNotice
      tone="danger"
      title="Capability data unavailable"
      actions={<Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {message}
    </ConsoleNotice>
  );
}
