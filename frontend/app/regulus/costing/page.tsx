"use client";

// The Regulus Costing screen — the econ control-plane's cost model + value view,
// reached ONLY through the admin-gated Regulus proxy (see lib/regulusApi.ts).
//
// Built on the P0 primitives in the P1/P2 house style (Metrics / Cost): inline
// styles + CSS-var tokens, dark-only, the mounted/connected gate, section heads,
// Skeleton loading, inline error+Retry. Nothing here crashes when the proxy is
// unconfigured, unreachable, admin-gated off, or missing the requested id.
//
// LAYOUT
//   1. Performance summary  — rgPerformanceSummary() (no id, fires on mount):
//      flat numeric fields surface as stat tiles; the full payload is always shown
//      as JSON. rgPerformanceCapabilities() renders as a defensively-derived table
//      (+ JSON). Both are typed `unknown` by the proxy, so this screen NEVER assumes
//      keys — a plain object → numeric tiles, an array of objects → a table, anything
//      else → JSON only.
//   2. Cost profile lookup  — text input (profile id) + Fetch → rgCostProfile(id).
//   3. Value estimate lookup — text input (capability id) + Fetch → rgEstimateLatest(id).
//      Lookups fire on SUBMIT (not mount); there is no list endpoint for either in the
//      proxy, hence the id form. A 404 for an unknown id degrades to a friendly
//      "nothing here" note, not a red error.
//
// FIELD MAPPING (renders only real wire fields — see api-types.regulus.ts):
//   CostProfileOut  : capability_id, id, requests_per_period, avg_input_tokens,
//     avg_output_tokens, infra_monthly_spend_usd, retry_rate, cache_hit_rate
//     (the four *_json override maps live in the JSON block, never assumed present).
//   ValueEstimateOut (aliased EstimateOut): capability_id, implementation_id,
//     period_start/period_end, estimated_value_usd, estimated_cost_usd, net_margin_usd,
//     credible_interval_low_usd/high_usd, confidence_level, relative_interval_width,
//     confidence_gate_passed, drift_score, drift_state, cost_data_quality,
//     value_data_quality, estimation_method_version, interval_method.
//   Every scalar access is guarded (finite-number / typeof) so a shape drift renders
//   "—" instead of throwing — and the full JSON payload is always shown below.
//
// The API key is applied only inside lib/api's apiFetch (X-API-Key header) — never
// logged, never placed in a URL.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  CodeBlock,
  ConsoleEmpty,
  ConsoleInput,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  ConsoleTableFrame,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { EconomicsPanel } from "@/app/regulus/EconomicsPanel";
import { EconomicsWorkspaceNav } from "@/app/components/EconomicsWorkspaceNav";
import { useLoad } from "@/app/hooks/useLoad";
import { ApiError, errMsg } from "@/app/lib/api";
import { fmtUsd as fmtKnownUsd } from "@/app/components/ui";
import {
  rgCostProfile,
  rgEstimateLatest,
  rgPerformanceCapabilities,
  rgPerformanceSummary,
  type CostProfileOut,
  type CostEstimate,
} from "@/app/lib/regulusApi";
import { isConfigured } from "@/app/lib/config";
import styles from "../subpages.module.css";

const MONO = "var(--font-mono)";
const MAX_TILES = 24; // cap the numeric-tile preview; the JSON block always has all of it
const MAX_ROWS = 50; // cap rendered capability rows; JSON block always has all of them

// --------------------------------------------------------------------------
// Formatters — every one guards a non-number / undefined so a runtime shape
// drift (or a missing optional) renders "—" instead of "$NaN" / a crash.
// --------------------------------------------------------------------------

function fmtNumber(n: number): string {
  const abs = Math.abs(n);
  if (abs !== 0 && (abs < 1e-4 || abs >= 1e15)) return n.toExponential(2);
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

function fmtUsd(n: unknown): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return fmtKnownUsd(n);
}

function fmtPct(n: unknown): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtInt(n: unknown): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

/** ISO timestamp → compact UTC "YYYY-MM-DD HH:mm Z"; raw string if unparseable. */
function fmtDate(s: unknown): string {
  if (typeof s !== "string" || !s) return "—";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  return `${d.toISOString().replace("T", " ").slice(0, 16)}Z`;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v != null && typeof v === "object" && !Array.isArray(v);
}

/** Pretty JSON, guarded against odd values (a JSON body can't be circular, but
 *  stringify still degrades to String() rather than throw). */
function toJson(data: unknown): string {
  try {
    return JSON.stringify(data, null, 2) ?? String(data);
  } catch {
    return String(data);
  }
}

/** Deterministic tone for an open data-quality string. */
function qualityTone(q: unknown): string {
  const s = typeof q === "string" ? q.toLowerCase() : "";
  if (s.includes("measured")) return "success";
  if (s.includes("mixed")) return "info";
  if (s.includes("inferred")) return "warning";
  return "neutral";
}

/** Tone for drift_state ("stable" | "warning" | "critical"), neutral if unknown. */
function driftTone(s: unknown): string {
  const t = typeof s === "string" ? s.toLowerCase() : "";
  if (t === "critical") return "danger";
  if (t === "warning") return "warning";
  if (t === "stable") return "success";
  return "neutral";
}

// --------------------------------------------------------------------------
// Page shell — mounted/connected gate (localStorage read after mount so the
// static prerender and first client render agree; sections mount only when
// connected, so their on-mount loaders don't fire against an unconfigured API).
// --------------------------------------------------------------------------

export default function CostingPage() {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Costing"
        description="Cost models and value estimates from the economic control plane."
      />

      <EconomicsWorkspaceNav active="models" />

      <ConsoleNotice title="Data context">
        Scope: platform-admin control plane · Window: latest profile or estimate · Source: Regulus ·
        Freshness: current request. Restricted data returns an authorization explanation in place.
      </ConsoleNotice>

      {!connected ? (
        <ConnectNote />
      ) : (
        <div className={styles.stack}>
          <PerformanceSection />
          <CostProfileLookup />
          <ValueEstimateLookup />
        </div>
      )}
    </ConsolePage>
  );
}

// --------------------------------------------------------------------------
// 1. Performance summary + capabilities (no id — both fire on mount)
// --------------------------------------------------------------------------

type Tile = { key: string; value: string };

function numericView(data: unknown): { tiles: Tile[]; overflow: number; json: string; isObject: boolean } {
  const json = toJson(data);
  if (!isPlainObject(data)) return { tiles: [], overflow: 0, json, isObject: false };
  const nums: Tile[] = [];
  for (const [key, value] of Object.entries(data)) {
    if (typeof value === "number" && Number.isFinite(value)) {
      nums.push({ key, value: fmtNumber(value) });
    }
  }
  const tiles = nums.slice(0, MAX_TILES);
  return { tiles, overflow: nums.length - tiles.length, json, isObject: true };
}

type CapsView =
  | { kind: "table"; columns: string[]; rows: Record<string, unknown>[]; total: number; json: string }
  | { kind: "empty"; json: string }
  | { kind: "json"; json: string };

function capsView(data: unknown): CapsView {
  const json = toJson(data);
  if (Array.isArray(data)) {
    if (data.length === 0) return { kind: "empty", json };
    if (data.every(isPlainObject)) {
      const rows = (data as Record<string, unknown>[]).slice(0, MAX_ROWS);
      const columns: string[] = [];
      for (const r of rows) {
        for (const k of Object.keys(r)) {
          if (!columns.includes(k)) columns.push(k);
          if (columns.length >= 8) break; // cap columns; JSON has the rest
        }
        if (columns.length >= 8) break;
      }
      return { kind: "table", columns, rows, total: data.length, json };
    }
  }
  return { kind: "json", json };
}

/** One table cell from an unknown value — primitives inline, objects as "{…}". */
function fmtCell(v: unknown): { text: string; title?: string } {
  if (v == null) return { text: "—" };
  if (typeof v === "number") return { text: Number.isFinite(v) ? fmtNumber(v) : String(v) };
  if (typeof v === "boolean") return { text: v ? "yes" : "no" };
  if (typeof v === "string") return { text: v || "—", title: v || undefined };
  const j = toJson(v);
  return { text: Array.isArray(v) ? `[${v.length}]` : "{…}", title: j };
}

function PerformanceSection() {
  const summary = useLoad<unknown>(rgPerformanceSummary);
  const caps = useLoad<unknown>(rgPerformanceCapabilities);

  const sv = useMemo(() => numericView(summary.data), [summary.data]);
  const cv = useMemo(() => capsView(caps.data), [caps.data]);

  const busy = summary.loading || caps.loading;

  return (
    <ConsoleSection
      title="Performance summary"
      actions={
        <Button
          onClick={() => {
            summary.reload();
            caps.reload();
          }}
          disabled={busy}
        >
          {busy ? "Loading…" : "Refresh"}
        </Button>
      }
    >

      <p className={styles.sectionNote}>
        Portfolio-level econ performance from the control plane. The body has no fixed schema;
        numeric fields are summarized first and the full payload remains available below.
      </p>

      {/* Summary */}
      {summary.loading && !summary.data ? (
        <ConsoleSurface>
          <div className={styles.metricGrid}>
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} height={58} />
            ))}
          </div>
          <div style={{ height: 12 }} />
          <Skeleton height={110} />
        </ConsoleSurface>
      ) : summary.error ? (
        <InlineError message={summary.error} onRetry={summary.reload} />
      ) : summary.data == null ? (
        <ConsoleEmpty>No performance summary reported.</ConsoleEmpty>
      ) : (
        <ConsoleSurface className={styles.resultStack}>
          {sv.tiles.length > 0 && (
            <div>
              <div className={styles.metricGrid}>
                {sv.tiles.map((t) => (
                  <Tile key={t.key} label={t.key} value={t.value} />
                ))}
              </div>
              {sv.overflow > 0 && (
                <p style={{ margin: "10px 0 0", fontSize: 11, color: "var(--text-faint)" }}>
                  +{sv.overflow} more numeric {sv.overflow === 1 ? "entry" : "entries"} in the payload below.
                </p>
              )}
            </div>
          )}
          <ScrollJson label="Summary (JSON)" code={sv.json} />
        </ConsoleSurface>
      )}

      {/* Capabilities */}
      <h3 className={styles.subheading}>Capabilities</h3>

      {caps.loading && !caps.data ? (
        <ConsoleSurface density="compact">
          <div className={styles.loadingStack}>
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} height={26} />
            ))}
          </div>
        </ConsoleSurface>
      ) : caps.error ? (
        <InlineError message={caps.error} onRetry={caps.reload} />
      ) : caps.data == null || cv.kind === "empty" ? (
        <ConsoleEmpty>No per-capability performance rows.</ConsoleEmpty>
      ) : cv.kind === "table" ? (
        <ConsoleTableFrame ariaLabel="Cost estimates">
            <table className={styles.table}>
              <thead>
                <tr>
                  {cv.columns.map((c) => (
                    <th
                      key={c}
                      style={{
                        textAlign: "left",
                        padding: "10px 14px",
                        borderBottom: "1px solid var(--hair)",
                        fontFamily: "var(--font-sans)",
                        fontSize: 11,
                        fontWeight: 500,
                        color: "var(--text-faint)",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cv.rows.map((row, i) => (
                  <tr key={i}>
                    {cv.columns.map((c) => {
                      const cell = fmtCell(row[c]);
                      return (
                        <td
                          key={c}
                          title={cell.title}
                          style={{
                            padding: "9px 14px",
                            borderBottom: "1px solid var(--hair)",
                            fontFamily: MONO,
                            fontSize: 12,
                            color: "var(--text-secondary)",
                            whiteSpace: "nowrap",
                            maxWidth: 260,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                          }}
                        >
                          {cell.text}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          {cv.total > cv.rows.length && (
            <p className={styles.overflowNote}>
              Showing {cv.rows.length} of {cv.total} rows — full set in the JSON below.
            </p>
          )}
          <div className={styles.jsonInset}>
            <ScrollJson label="Capabilities (JSON)" code={cv.json} />
          </div>
        </ConsoleTableFrame>
      ) : (
        <ConsoleSurface>
          <ScrollJson label="Capabilities (JSON)" code={cv.json} />
        </ConsoleSurface>
      )}
    </ConsoleSection>
  );
}

// --------------------------------------------------------------------------
// 2 + 3. Id-lookup cards. No list endpoint exists in the proxy for profiles or
// estimates, so each is a text input that fetches on submit. Generic over the
// fetched shape; a 404 renders as a friendly "nothing here" note (not an error).
// --------------------------------------------------------------------------

type LookupPhase =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "done"; data: unknown }
  | { phase: "error"; message: string; notFound: boolean };

function LookupCard<T>({
  label,
  hint,
  inputLabel,
  placeholder,
  notFoundHint,
  idleHint,
  jsonLabel,
  fetcher,
  renderResult,
}: {
  label: string;
  hint: string;
  inputLabel: string;
  placeholder: string;
  notFoundHint: (id: string) => string;
  idleHint: string;
  jsonLabel: string;
  fetcher: (id: string) => Promise<T>;
  renderResult: (data: T) => React.ReactNode;
}) {
  const [id, setId] = useState("");
  const [state, setState] = useState<LookupPhase>({ phase: "idle" });
  const [hintMsg, setHintMsg] = useState<string | null>(null);
  const lastId = useRef("");

  async function run(rawId: string) {
    const trimmed = rawId.trim();
    if (!trimmed) {
      setHintMsg(`Enter a ${inputLabel.toLowerCase()} first.`);
      return;
    }
    setHintMsg(null);
    lastId.current = trimmed;
    setState({ phase: "loading" });
    try {
      const data = await fetcher(trimmed);
      setState({ phase: "done", data });
    } catch (e) {
      const notFound = e instanceof ApiError && e.status === 404;
      setState({ phase: "error", message: errMsg(e), notFound });
    }
  }

  return (
    <EconomicsPanel title={label} evidenceScope={label}>
      <p className={styles.sectionNote}>
        {hint}
      </p>

      <div className={styles.resultStack}>
        <div className={styles.toolbar}>
          <ConsoleInput
            value={id}
            onChange={(e) => setId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && state.phase !== "loading") run(id);
            }}
            placeholder={placeholder}
            aria-label={inputLabel}
            spellCheck={false}
            autoComplete="off"
          />
          <Button variant="primary" onClick={() => run(id)} disabled={state.phase === "loading"}>
            {state.phase === "loading" ? "Fetching…" : "Fetch"}
          </Button>
        </div>

        {hintMsg && <EmptyInline>{hintMsg}</EmptyInline>}

        {state.phase === "loading" ? (
          <div className={styles.loadingStack}>
            <div className={styles.metricGrid}>
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} height={58} />
              ))}
            </div>
            <Skeleton height={110} />
          </div>
        ) : state.phase === "error" ? (
          state.notFound ? (
            <NotFoundNote message={notFoundHint(lastId.current)} onRetry={() => run(lastId.current)} />
          ) : (
            <InlineError message={state.message} onRetry={() => run(lastId.current)} />
          )
        ) : state.phase === "done" ? (
          <div className={styles.resultStack}>
            {renderResult(state.data as T)}
            <ScrollJson label={jsonLabel} code={toJson(state.data)} />
          </div>
        ) : (
          <EmptyInline>{idleHint}</EmptyInline>
        )}
      </div>
    </EconomicsPanel>
  );
}

function CostProfileLookup() {
  return (
    <LookupCard<CostProfileOut>
      label="Cost profile lookup"
      hint="Fetch a capability's cost profile by its numeric profile id — the token/tool/infra assumptions the estimator prices against."
      inputLabel="Profile id"
      placeholder="Profile id — e.g. 42"
      idleHint="Enter a profile id and Fetch to load its cost model."
      notFoundHint={(id) => `No cost profile for id "${id}".`}
      jsonLabel="Cost profile (JSON)"
      fetcher={rgCostProfile}
      renderResult={renderProfile}
    />
  );
}

function ValueEstimateLookup() {
  return (
    <LookupCard<CostEstimate>
      label="Cost estimate lookup"
      hint="Fetch the latest cost estimate for a capability id — modeled LLM / tool / infra / overhead cost and its interval."
      inputLabel="Capability id"
      placeholder="Capability id — e.g. cap_fraud_scoring"
      idleHint="Enter a capability id and Fetch to load its latest cost estimate."
      notFoundHint={(id) => `No cost estimate for capability "${id}".`}
      jsonLabel="Cost estimate (JSON)"
      fetcher={rgEstimateLatest}
      renderResult={renderEstimate}
    />
  );
}

// --------------------------------------------------------------------------
// Typed field renderers. Each access is guarded via the formatters above, so a
// missing/undefined field renders "—" rather than throwing; the JSON block that
// LookupCard appends always shows the real payload verbatim.
// --------------------------------------------------------------------------

function renderProfile(d: CostProfileOut) {
  return (
    <div className={styles.metricGrid}>
      <Tile label="capability_id" value={typeof d.capability_id === "string" ? d.capability_id : "—"} />
      <Tile label="profile id" value={fmtInt(d.id)} />
      <Tile label="requests / period" value={fmtInt(d.requests_per_period)} />
      <Tile label="avg input tokens" value={fmtInt(d.avg_input_tokens)} />
      <Tile label="avg output tokens" value={fmtInt(d.avg_output_tokens)} />
      <Tile label="infra monthly" value={fmtUsd(d.infra_monthly_spend_usd)} />
      <Tile label="retry rate" value={fmtPct(d.retry_rate)} />
      <Tile label="cache hit rate" value={fmtPct(d.cache_hit_rate)} />
    </div>
  );
}

function renderEstimate(d: CostEstimate) {
  const impl =
    typeof d.implementation_id === "string" && d.implementation_id
      ? ` · ${d.implementation_id}`
      : "";

  return (
    <div className={styles.resultStack}>
      {/* Headline: total cost estimate */}
      <div>
        <div style={{ fontFamily: "var(--font-sans)", fontVariantNumeric: "tabular-nums", fontSize: 24, fontWeight: 500, lineHeight: 1.1 }}>
          {fmtUsd(d.total_cost_estimate_usd)}
        </div>
        <div
          style={{
            marginTop: 4,
            fontFamily: MONO,
            fontSize: 11,
            color: "var(--text-faint)",
            overflowWrap: "anywhere",
          }}
        >
          total cost estimate · {typeof d.capability_id === "string" ? d.capability_id : "—"}
          {impl}
        </div>
      </div>

      {/* Cost breakdown */}
      <div className={styles.metricGrid}>
        <Tile label="llm cost" value={fmtUsd(d.llm_cost_estimate_usd)} />
        <Tile label="tool cost" value={fmtUsd(d.tool_cost_estimate_usd)} />
        <Tile label="infra cost" value={fmtUsd(d.infra_cost_estimate_usd)} />
        <Tile label="overhead cost" value={fmtUsd(d.overhead_cost_estimate_usd)} />
        <Tile label="interval low" value={fmtUsd(d.cost_interval_low_usd)} />
        <Tile label="interval high" value={fmtUsd(d.cost_interval_high_usd)} />
      </div>

      {/* Method / quality pills */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {typeof d.data_quality === "string" && (
          <Pill tone={qualityTone(d.data_quality)} title="data_quality">
            data {d.data_quality}
          </Pill>
        )}
        {typeof d.estimation_method === "string" && (
          <Pill tone="neutral" title="estimation_method">
            {d.estimation_method}
          </Pill>
        )}
        {typeof d.method_version === "string" && (
          <Pill tone="neutral" title="method_version">
            v{d.method_version}
          </Pill>
        )}
      </div>

      {/* Period */}
      <div
        style={{
          fontFamily: "var(--font-sans)",
          fontSize: 11,
          color: "var(--text-faint)",
          overflowWrap: "anywhere",
        }}
      >
        period {fmtDate(d.period_start)} → {fmtDate(d.period_end)}
      </div>
    </div>
  );
}

function GatePill({ passed }: { passed: unknown }) {
  if (typeof passed !== "boolean") {
    return <Pill tone="neutral">gate n/a</Pill>;
  }
  const tone = passed ? "success" : "danger";
  return (
    <Pill tone={tone} title="confidence_gate_passed">
      <StatusDot tone={tone} />
      {passed ? "gate passed" : "gate blocked"}
    </Pill>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirror the Metrics / Cost screens)
// --------------------------------------------------------------------------

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.metricTile}>
      <div
        title={value}
        className={styles.metricValue}
      >
        {value}
      </div>
      <div
        title={label}
        className={styles.metricLabel}
      >
        {label}
      </div>
    </div>
  );
}

/** CodeBlock capped in height so a large payload scrolls inside the card. */
function ScrollJson({ label, code }: { label: string; code: string }) {
  return (
    <div style={{ maxHeight: 420, overflow: "auto" }}>
      <CodeBlock label={label} code={code} />
    </div>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <p className={styles.emptyInline}>{children}</p>;
}

function ConnectNote() {
  return (
    <ConsoleNotice title="Not connected">
      Open Connect in the sidebar to set the API base and key. Costing reads through
      the admin-gated Regulus proxy.
    </ConsoleNotice>
  );
}

/** Soft "nothing here for that id" note — a 404 is an expected miss on a lookup,
 *  not a failure, so it reads neutral rather than red. Retry re-runs the same id. */
function NotFoundNote({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <ConsoleNotice
      title="No matching record"
      actions={<Button variant="neutral" onClick={onRetry}>Try again</Button>}
    >
      {message}
    </ConsoleNotice>
  );
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  const restricted = message.startsWith("403");
  return (
    <ConsoleNotice
      tone={restricted ? undefined : "danger"}
      title={restricted ? "Access restricted" : "Costing data unavailable"}
      actions={restricted ? undefined : <Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {restricted
        ? "This API key cannot read platform cost models. Connect with a metrics-read or platform-admin credential."
        : message}
    </ConsoleNotice>
  );
}
