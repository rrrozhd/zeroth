"use client";

// The Metrics screen — a small observability view over two surfaces the connected
// service exposes: the runtime metrics snapshot (GET /v1/metrics) and the manifest
// registry (GET /v1/manifests).
//
// Built on the P0 primitives, matching the P1/P2 house style (Connectors / Templates):
// inline styles + CSS-var tokens, dark-only, the useLoad + mounted/connected gate,
// section heads, and an inline error+Retry state. Nothing here crashes when the API
// is unconfigured or unreachable — useLoad turns failures into an inline error state.
//
// OPEN METRICS PAYLOAD (see api-types.ts: MetricsResponse is `unknown` — /v1/metrics
// has no fixed schema). readMetrics() is defensive about the shape and never assumes
// keys:
//   - a string (e.g. Prometheus exposition text) is rendered verbatim in the CodeBlock;
//   - a plain object surfaces its TOP-LEVEL finite-number entries as stat tiles AND is
//     pretty-printed into the CodeBlock;
//   - anything else (array, number, null, …) is just pretty-printed JSON.
// The full payload ALWAYS lands in the CodeBlock regardless of shape — the tiles are
// only a convenience preview.
//
// FIELD MAPPING (renders only real wire fields — see api-types.ts):
//   ManifestSummaryResponse: manifest_ref (name), kind (the categorical badge — a
//     manifest has no lifecycle "status"), runtime (nullable), description (nullable).
//
// Browser authentication uses a short-lived HttpOnly session cookie.

import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  CodeBlock,
  MonoLabel,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { useLoad } from "@/app/hooks/useLoad";
import {
  ApiError,
  getMetrics,
  listManifests,
  type ManifestSummary,
  type MetricsResponse,
} from "@/app/lib/api";
import { getApiBase, isConfigured } from "@/app/lib/config";
import { ManifestInspector } from "./ManifestInspector";

// --------------------------------------------------------------------------
// Open-payload normalization — the whole point of this screen's defensiveness.
// --------------------------------------------------------------------------

type Tile = { key: string; value: string };

/** Cap the tile preview so a metrics dict with many numeric keys can't spawn a
 *  runaway grid — the full payload is always in the CodeBlock below anyway. */
const MAX_TILES = 24;

/** Human-friendly number: grouping separators, ≤4 fractional digits, and an
 *  exponential fallback for the very tiny / very large so a tile never overflows. */
function fmtNumber(n: number): string {
  const abs = Math.abs(n);
  if (abs !== 0 && (abs < 1e-4 || abs >= 1e15)) return n.toExponential(2);
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

type MetricsView = {
  /** Text for the CodeBlock — the raw string, or pretty-printed JSON. */
  text: string;
  /** Top-level numeric entries as tiles (capped at MAX_TILES). */
  tiles: Tile[];
  /** How many numeric entries were dropped past the cap. */
  overflow: number;
  /** What the payload turned out to be — drives the CodeBlock label. */
  kind: "string" | "object" | "other";
};

/** Turn the open /v1/metrics body into something renderable without ever assuming
 *  its shape. A JSON body cannot be circular (it came from JSON.parse), but the
 *  stringify is still guarded so a hostile/odd value degrades to String(). */
function readMetrics(data: unknown): MetricsView {
  if (typeof data === "string") {
    return { text: data, tiles: [], overflow: 0, kind: "string" };
  }

  let text: string;
  try {
    text = JSON.stringify(data, null, 2) ?? String(data);
  } catch {
    text = String(data);
  }

  const isPlainObject =
    data != null && typeof data === "object" && !Array.isArray(data);
  if (!isPlainObject) {
    return { text, tiles: [], overflow: 0, kind: "other" };
  }

  const numeric: Tile[] = [];
  for (const [key, value] of Object.entries(data as Record<string, unknown>)) {
    if (typeof value === "number" && Number.isFinite(value)) {
      numeric.push({ key, value: fmtNumber(value) });
    }
  }
  const tiles = numeric.slice(0, MAX_TILES);
  return { text, tiles, overflow: numeric.length - tiles.length, kind: "object" };
}

/** Load the open metrics body. getMetrics() is the primary path, but shared
 *  apiFetch() always parses the body as JSON — so a text exposition (Prometheus:
 *  `# TYPE …`) makes it throw a raw SyntaxError, NOT an ApiError. Network/HTTP
 *  failures come back as ApiError and are genuinely fatal (rethrow → error state);
 *  a non-ApiError means the body arrived but wasn't JSON, so refetch and read it
 *  as text. Authentication uses the short-lived HttpOnly session cookie; the
 *  exchanged API key is never persisted, logged, or placed in the URL. */
async function loadMetrics(): Promise<MetricsResponse> {
  try {
    return await getMetrics();
  } catch (e) {
    if (e instanceof ApiError) throw e;
    return await fetchMetricsText();
  }
}

async function fetchMetricsText(): Promise<string> {
  const base = getApiBase();
  const headers: Record<string, string> = { Accept: "text/plain, */*" };
  const res = await fetch(`${base}/v1/metrics`, {
    headers,
    credentials: "include",
    signal: AbortSignal.timeout(20_000),
  });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  return res.text();
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function MetricsPage() {
  // localStorage-derived config is read after mount so the static prerender and
  // the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  return (
    <div style={{ maxWidth: 980, margin: "0 auto", padding: "28px 28px 48px" }}>
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Metrics</h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          Runtime snapshot for the connected service, and the manifests it can resolve.
        </p>
      </header>

      <MetricsSection connected={connected} mounted={mounted} />
      <div style={{ height: 24 }} />
      <ManifestsSection connected={connected} mounted={mounted} />
    </div>
  );
}

// --------------------------------------------------------------------------
// Metrics — numeric tiles (best-effort) + the always-rendered raw payload.
// --------------------------------------------------------------------------

function MetricsSection({ connected, mounted }: { connected: boolean; mounted: boolean }) {
  const metrics = useLoad<MetricsResponse>(loadMetrics);
  const view = useMemo(() => readMetrics(metrics.data), [metrics.data]);

  return (
    <section data-evidence-scope="runtime-metrics">
      <SectionHead label="Metrics">
        {connected && (
          <Button
            onClick={metrics.reload}
            disabled={metrics.loading}
            style={{ padding: "4px 9px" }}
          >
            {metrics.loading ? "Loading…" : "Refresh"}
          </Button>
        )}
      </SectionHead>

      <p style={{ margin: "0 0 12px", fontSize: 12, color: "var(--text-faint)", lineHeight: 1.55 }}>
        The service&rsquo;s runtime metrics snapshot. The body has no fixed schema — numeric
        top-level entries are surfaced as tiles; the full payload is always shown below.
      </p>

      {metrics.loading && !metrics.data ? (
        <Card pad={16}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
              gap: 10,
            }}
          >
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} height={58} />
            ))}
          </div>
          <div style={{ height: 12 }} />
          <Skeleton height={120} />
        </Card>
      ) : metrics.error ? (
        <InlineError message={metrics.error} onRetry={metrics.reload} />
      ) : mounted && !connected ? (
        <Card pad={16}>
          <EmptyInline>Connect to the API (top bar) to load metrics.</EmptyInline>
        </Card>
      ) : metrics.data == null ? (
        <Card pad={16}>
          <EmptyInline>No metrics reported by this service.</EmptyInline>
        </Card>
      ) : (
        <Card pad={16} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {view.tiles.length > 0 && (
            <div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
                  gap: 10,
                }}
              >
                {view.tiles.map((t) => (
                  <StatTile key={t.key} label={t.key} value={t.value} />
                ))}
              </div>
              {view.overflow > 0 && (
                <p style={{ margin: "10px 0 0", fontSize: 11, color: "var(--text-faint)" }}>
                  +{view.overflow} more numeric{" "}
                  {view.overflow === 1 ? "entry" : "entries"} in the payload below.
                </p>
              )}
            </div>
          )}

          {/* Cap the height so a large exposition dump scrolls inside the card
              instead of stretching the page. */}
          <div style={{ maxHeight: 460, overflow: "auto" }}>
            <CodeBlock
              label={view.kind === "string" ? "Raw payload" : "Payload (JSON)"}
              code={view.text}
            />
          </div>
        </Card>
      )}
    </section>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        minWidth: 0,
        background: "var(--bg-raised)",
        border: "1px solid var(--hair)",
        borderRadius: 6,
        padding: "10px 12px",
      }}
    >
      <div
        title={value}
        style={{
          fontFamily: "var(--font-sans)",
          fontSize: 18,
          fontWeight: 500,
          color: "var(--text-primary)",
          fontVariantNumeric: "tabular-nums",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {value}
      </div>
      <div
        title={label}
        style={{
          marginTop: 4,
          fontFamily: "var(--font-sans)",
          fontSize: 11,
          fontWeight: 500,
          color: "var(--text-faint)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {label}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Manifests — a table over the resolvable manifest_ref registry.
// name (manifest_ref) | kind | runtime | description.
// minmax(0,…) on every track so long refs/descriptions ellipsize instead of
// forcing horizontal page scroll.
// --------------------------------------------------------------------------

const MANIFEST_COLS =
  "minmax(0,1.6fr) minmax(0,1.15fr) minmax(0,0.9fr) minmax(0,1.9fr) minmax(84px,auto)";

function ManifestsSection({ connected, mounted }: { connected: boolean; mounted: boolean }) {
  const manifests = useLoad<ManifestSummary[]>(listManifests);
  const rows = manifests.data ?? [];
  const [selectedRef, setSelectedRef] = useState<string | null>(null);

  return (
    <section data-evidence-scope="manifests">
      <SectionHead label="Manifests">
        {connected && (
          <Button
            onClick={manifests.reload}
            disabled={manifests.loading}
            style={{ padding: "4px 9px" }}
          >
            {manifests.loading ? "Loading…" : "Refresh"}
          </Button>
        )}
      </SectionHead>

      <p style={{ margin: "0 0 12px", fontSize: 12, color: "var(--text-faint)", lineHeight: 1.55 }}>
        Executable units and agent runners registered for this deployment — the resolvable{" "}
        <code style={{ fontFamily: "var(--font-mono)" }}>manifest_ref</code> values an
        executable-unit node can reference and the runner names agent nodes bind to.
      </p>

      {manifests.loading && !manifests.data ? (
        <Card pad={14}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} height={30} />
            ))}
          </div>
        </Card>
      ) : manifests.error ? (
        <InlineError message={manifests.error} onRetry={manifests.reload} />
      ) : mounted && !connected ? (
        <Card pad={16}>
          <EmptyInline>Connect to the API (top bar) to load manifests.</EmptyInline>
        </Card>
      ) : rows.length === 0 ? (
        <Card pad={16}>
          <EmptyInline>No manifests registered for this deployment.</EmptyInline>
        </Card>
      ) : (
        <Card pad={0} style={{ overflow: "hidden" }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: MANIFEST_COLS,
              gap: 12,
              padding: "10px 16px",
              borderBottom: "1px solid var(--hair)",
            }}
          >
            <ColHead>name</ColHead>
            <ColHead>kind</ColHead>
            <ColHead>runtime</ColHead>
            <ColHead>description</ColHead>
            <span />
          </div>
          {rows.map((m) => (
            <ManifestRow
              key={m.manifest_ref}
              manifest={m}
              selected={selectedRef === m.manifest_ref}
              onInspect={() =>
                setSelectedRef((current) => current === m.manifest_ref ? null : m.manifest_ref)
              }
            />
          ))}
        </Card>
      )}
    </section>
  );
}

/** kind is an open string; tone it deterministically, defaulting to neutral so an
 *  unknown kind still renders a coherent badge. */
function kindTone(kind: string): string {
  const k = kind.toLowerCase();
  if (k.includes("agent")) return "agent";
  if (k.includes("exec")) return "accent";
  if (k.includes("tool")) return "info";
  return "neutral";
}

function ManifestRow({
  manifest: m,
  selected,
  onInspect,
}: {
  manifest: ManifestSummary;
  selected: boolean;
  onInspect: () => void;
}) {
  const tone = kindTone(m.kind);
  return (
    <>
      <div
        data-evidence-scope={`manifest-row-${m.manifest_ref}`}
        style={{
          display: "grid",
          gridTemplateColumns: MANIFEST_COLS,
          gap: 12,
          alignItems: "center",
          padding: "11px 16px",
          borderBottom: "1px solid var(--hair)",
          background: selected ? "color-mix(in srgb, var(--accent) 5%, transparent)" : undefined,
        }}
      >
      {/* name (manifest_ref) */}
      <span
        title={m.manifest_ref}
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
        {m.manifest_ref}
      </span>

      {/* kind — the categorical badge (a manifest has no lifecycle status) */}
      <div style={{ minWidth: 0 }}>
        <Pill tone={tone} title={m.kind} style={{ maxWidth: "100%", overflow: "hidden" }}>
          <StatusDot tone={tone} />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{m.kind}</span>
        </Pill>
      </div>

      {/* runtime (nullable) */}
      <CellText mono title={m.runtime ?? undefined}>
        {m.runtime ?? "—"}
      </CellText>

      {/* description (nullable) */}
        <CellText title={m.description ?? undefined}>{m.description ?? "—"}</CellText>

        {m.kind === "executable_unit" ? (
          <Button
            onClick={onInspect}
            aria-expanded={selected}
            aria-label={`${selected ? "Close" : "Inspect"} ${m.manifest_ref}`}
            style={{ padding: "4px 9px" }}
          >
            {selected ? "Close" : "Inspect"}
          </Button>
        ) : (
          <span aria-hidden="true" style={{ color: "var(--text-faint)" }}>—</span>
        )}
      </div>
      {selected && <ManifestInspector manifestRef={m.manifest_ref} />}
    </>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirrors the Connectors / Templates screen conventions)
// --------------------------------------------------------------------------

function SectionHead({ label, children }: { label: string; children?: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 8,
        marginBottom: 10,
      }}
    >
      <MonoLabel>{label}</MonoLabel>
      {children}
    </div>
  );
}

function ColHead({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 10,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.08em",
        color: "var(--text-faint)",
      }}
    >
      {children}
    </span>
  );
}

function CellText({
  children,
  mono = false,
  title,
}: {
  children: React.ReactNode;
  mono?: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      style={{
        display: "block",
        minWidth: 0,
        fontFamily: mono ? "var(--font-mono)" : "var(--font-sans)",
        fontSize: 12,
        color: "var(--text-secondary)",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {children}
    </span>
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>{children}</div>;
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
