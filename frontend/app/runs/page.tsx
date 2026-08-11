"use client";

// The Runs screen — a master-detail operator view over the live run API.
//
// Left: a filterable list of runs (listRuns). Right: the selected run's detail
// (getRun) with admin actions, a per-node timeline (getRunTimeline), an evidence
// bundle (getRunEvidence), and audit-chain verification (verifyRunChain).
//
// Every read happens client-side and degrades gracefully: an unconfigured or
// unreachable API surfaces as an inline error (with Retry) or an empty state,
// never a crash. The API key lives only in localStorage (lib/config) — it is
// never logged and never placed in a URL; the Invoke cURL block shows a
// redacted `$ZEROTH_API_KEY` placeholder, not the real secret.
//
// NOTE ON DERIVED FIELDS: RunStatusResponse carries no run-level cost or
// timestamps, so the detail's cost/started are summed / min'd from the run's
// NodeAuditRecord timeline entries (cost_usd, started_at) — the only real
// sources. The list rows show only graph@version for the same reason.

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  CodeBlock,
  MonoLabel,
  nodeTypeColor,
  Pill,
  Skeleton,
  StatusDot,
  TONE,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useAuditVerification } from "@/app/components/auditVerificationContext";
import { RUN_TONE, runStatusLabel } from "@/app/components/runTone";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import { usePolling } from "@/app/hooks/usePolling";
import {
  cancelRun,
  errMsg,
  getRun,
  getRunEvidence,
  getRunTimeline,
  interruptRun,
  listRuns,
  replayRun,
  verifyRunChain,
  type AdminRunList,
  type AuditTimeline,
  type AuditVerification,
  type NodeAuditRecord,
  type RunEvidence,
  type RunStatus,
} from "@/app/lib/api";
import { getApiBase, isConfigured } from "@/app/lib/config";

// Statuses that keep the detail (getRun + getRunTimeline) and the list polling
// live — the run is still in flight and its nodes can still advance.
const LIVE = new Set<string>(["running", "queued", "paused_for_approval"]);

// Cancel is offered only while the run is running or holding at an approval;
// Interrupt only while actively running (per handoff §3).
const CANCELLABLE = new Set<string>(["running", "paused_for_approval"]);

// The "failed" filter groups every terminal-bad state (all map to danger tone).
const DANGER_STATES = new Set<string>(
  Object.entries(RUN_TONE)
    .filter(([, tone]) => tone === "danger")
    .map(([status]) => status),
);

type FilterId = "all" | "running" | "succeeded" | "failed" | "awaiting";

const FILTERS: { id: FilterId; label: string; match: (s: string) => boolean }[] = [
  { id: "all", label: "all", match: () => true },
  { id: "running", label: "running", match: (s) => s === "running" },
  { id: "succeeded", label: "succeeded", match: (s) => s === "succeeded" },
  { id: "failed", label: "failed", match: (s) => DANGER_STATES.has(s) },
  { id: "awaiting", label: "awaiting approval", match: (s) => s === "paused_for_approval" },
];

// Node audit-record status -> tone. Records are written on node completion, so
// in practice these are completed/failed/rejected; running/queued are handled
// defensively for the live-advance case.
const NODE_TONE: Record<string, string> = {
  completed: "success",
  succeeded: "success",
  success: "success",
  running: "info",
  in_progress: "info",
  queued: "muted",
  pending: "muted",
  failed: "danger",
  error: "danger",
  rejected: "danger",
  skipped: "neutral",
};
const NODE_RUNNING = new Set<string>(["running", "in_progress"]);
const NODE_QUEUED = new Set<string>(["queued", "pending"]);

function toneColor(tone: string): string {
  return TONE[tone] ?? tone;
}

function runTone(status: string): string {
  return RUN_TONE[status] ?? "neutral";
}

function nodeTypeOf(rec: NodeAuditRecord): string | null {
  const meta = rec.execution_metadata as Record<string, unknown> | undefined;
  const t = meta?.node_type;
  return typeof t === "string" ? t : null;
}

function fmtCost(n: number): string {
  return `$${n.toFixed(4)}`;
}

function fmtTimeOfDay(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour12: false });
}

function fmtDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], { hour12: false });
}

function fmtDuration(startIso?: string | null, endIso?: string | null): string | null {
  if (!startIso || !endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`;
}

// --------------------------------------------------------------------------
// Page shell — Suspense wraps useSearchParams (required under static export).
// --------------------------------------------------------------------------

export default function RunsPage() {
  return (
    <Suspense fallback={<div style={{ height: "100%" }} />}>
      <RunsView />
    </Suspense>
  );
}

function RunsView() {
  const params = useSearchParams();
  const runs = useLoad<AdminRunList>(listRuns);

  // Deep-link: `?run=<id>` selects a run on mount (Overview / canvas jump here).
  const [selected, setSelected] = useState<string | null>(() => params.get("run"));
  const [filter, setFilter] = useState<FilterId>("all");

  // localStorage-derived config is read after mount so the static prerender and
  // the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  // Keep the URL shareable/reload-safe without a full navigation.
  function select(id: string | null) {
    setSelected(id);
    if (typeof window !== "undefined") {
      const base = window.location.pathname;
      window.history.replaceState(null, "", id ? `${base}?run=${encodeURIComponent(id)}` : base);
    }
  }

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0 }}>
      <RunListPane
        runs={runs}
        connected={connected}
        mounted={mounted}
        filter={filter}
        onFilter={setFilter}
        selected={selected}
        onSelect={select}
      />
      <div style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
        {selected ? (
          <RunDetail
            key={selected}
            runId={selected}
            onSelectRun={select}
            onListChanged={runs.reload}
          />
        ) : (
          <DetailPlaceholder />
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Left list (330px)
// --------------------------------------------------------------------------

function RunListPane({
  runs,
  connected,
  mounted,
  filter,
  onFilter,
  selected,
  onSelect,
}: {
  runs: Loadable<AdminRunList>;
  connected: boolean;
  mounted: boolean;
  filter: FilterId;
  onFilter: (f: FilterId) => void;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const active = FILTERS.find((f) => f.id === filter) ?? FILTERS[0];
  const all = runs.data?.runs ?? [];
  const shown = useMemo(() => all.filter((r) => active.match(r.status)), [all, active]);

  return (
    <aside
      style={{
        width: 330,
        flexShrink: 0,
        borderRight: "1px solid var(--hair)",
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        background: "var(--bg-chrome)",
      }}
    >
      {/* Filter chips */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "14px 14px 10px",
          borderBottom: "1px solid var(--hair)",
        }}
      >
        {FILTERS.map((f) => {
          const on = f.id === filter;
          return (
            <button
              key={f.id}
              type="button"
              onClick={() => onFilter(f.id)}
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 11,
                letterSpacing: "0.02em",
                padding: "3px 9px",
                borderRadius: 5,
                cursor: "pointer",
                textTransform: "lowercase",
                color: on ? "var(--accent)" : "var(--text-muted)",
                background: on ? "rgba(94,234,212,0.10)" : "transparent",
                border: `1px solid ${on ? "rgba(94,234,212,0.30)" : "var(--hair)"}`,
                transition: "color 120ms ease, background 120ms ease",
              }}
            >
              {f.label}
            </button>
          );
        })}
      </div>

      {/* Rows */}
      <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
        {runs.loading && !runs.data ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 14 }}>
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} height={34} />
            ))}
          </div>
        ) : runs.error ? (
          <div style={{ padding: 14 }}>
            <InlineError message={runs.error} onRetry={runs.reload} />
          </div>
        ) : mounted && !connected ? (
          <EmptyNote>
            Connect to the API (top bar) to load runs.
          </EmptyNote>
        ) : all.length === 0 ? (
          <EmptyNote>No runs yet.</EmptyNote>
        ) : shown.length === 0 ? (
          <EmptyNote>No {active.label} runs.</EmptyNote>
        ) : (
          shown.map((r) => (
            <RunRow
              key={r.run_id}
              run={r}
              selected={r.run_id === selected}
              onSelect={() => onSelect(r.run_id)}
            />
          ))
        )}
      </div>
    </aside>
  );
}

function RunRow({
  run,
  selected,
  onSelect,
}: {
  run: RunStatus;
  selected: boolean;
  onSelect: () => void;
}) {
  const tone = runTone(run.status);
  return (
    <button
      type="button"
      onClick={onSelect}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        cursor: "pointer",
        padding: "10px 14px",
        border: "none",
        borderLeft: `2px solid ${selected ? "var(--accent)" : "transparent"}`,
        borderBottom: "1px solid var(--hair)",
        background: selected ? "rgba(94,234,212,0.07)" : "transparent",
        color: "inherit",
        transition: "background 120ms ease",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <StatusDot tone={tone} pulse={run.status === "running"} />
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            color: "var(--text-primary)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            flex: 1,
            minWidth: 0,
          }}
        >
          {run.run_id}
        </span>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 9.5,
            fontWeight: 500,
            letterSpacing: "0.05em",
            textTransform: "uppercase",
            color: toneColor(tone),
            flexShrink: 0,
          }}
        >
          {runStatusLabel(run.status)}
        </span>
      </div>
      <div
        style={{
          marginTop: 4,
          marginLeft: 16,
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-faint)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {run.graph_version_ref}
      </div>
    </button>
  );
}

// --------------------------------------------------------------------------
// Detail
// --------------------------------------------------------------------------

function DetailPlaceholder() {
  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--text-faint)",
        fontSize: 13,
      }}
    >
      Select a run to inspect.
    </div>
  );
}

function RunDetail({
  runId,
  onSelectRun,
  onListChanged,
}: {
  runId: string;
  onSelectRun: (id: string) => void;
  onListChanged: () => void;
}) {
  const run = useLoad<RunStatus>(() => getRun(runId));
  const timeline = useLoad<AuditTimeline>(() => getRunTimeline(runId));
  const toast = useToast();
  const [busy, setBusy] = useState<null | "cancel" | "interrupt" | "replay">(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const status = run.data?.status ?? "";
  const live = LIVE.has(status);

  // While the run is in flight, refetch detail + timeline (~2s) so nodes advance
  // live, and refresh the master list so its row keeps pace.
  usePolling(
    () => {
      run.reload();
      timeline.reload();
      onListChanged();
    },
    2000,
    live,
  );

  const entries = useMemo(() => timeline.data?.entries ?? [], [timeline.data]);
  const runCost = entries.reduce((a, e) => a + (e.cost_usd ?? 0), 0);
  const hasCost = entries.some((e) => e.cost_usd != null);
  const startedAt = entries
    .map((e) => e.started_at)
    .filter((s): s is string => !!s)
    .reduce<string | null>((min, s) => (min === null || s < min ? s : min), null);

  async function act(
    kind: "cancel" | "interrupt",
    fn: () => Promise<RunStatus>,
    verb: string,
  ) {
    setBusy(kind);
    setActionError(null);
    try {
      await fn();
      toast(`${verb} ${runId}`);
      run.reload();
      timeline.reload();
      onListChanged();
    } catch (e) {
      const message = `${kind === "cancel" ? "Cancel" : "Interrupt"} failed: ${errMsg(e)}`;
      setActionError(message);
      toast(message);
    } finally {
      setBusy(null);
    }
  }

  async function doCancel() {
    if (!window.confirm(`Cancel run ${runId}?`)) return;
    await act("cancel", () => cancelRun(runId), "Cancelled");
  }

  async function doReplay() {
    setBusy("replay");
    try {
      const resp = await replayRun(runId);
      toast(`Replayed → ${resp.run_id}`);
      onListChanged();
      onSelectRun(resp.run_id);
    } catch (e) {
      toast(`Replay failed: ${errMsg(e)}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div style={{ padding: "22px 26px", display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Header: id + status + actions */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 17, fontWeight: 600 }}>
          {runId}
        </span>
        {run.data && (
          <Pill tone={runTone(run.data.status)}>{runStatusLabel(run.data.status)}</Pill>
        )}
        {live && <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>live · refreshing…</span>}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          {CANCELLABLE.has(status) && (
            <Button
              variant="danger"
              disabled={busy !== null}
              onClick={doCancel}
            >
              {busy === "cancel" ? "…" : "Cancel"}
            </Button>
          )}
          {status === "running" && (
            <Button
              variant="neutral"
              disabled={busy !== null}
              onClick={() => act("interrupt", () => interruptRun(runId), "Interrupted")}
            >
              {busy === "interrupt" ? "…" : "Interrupt"}
            </Button>
          )}
          <Button variant="neutral" disabled={busy !== null} onClick={doReplay}>
            {busy === "replay" ? "…" : "Replay"}
          </Button>
        </div>
      </div>

      {actionError && (
        <div
          role="alert"
          style={{
            background: "rgba(248,113,113,0.08)",
            border: "1px solid rgba(248,113,113,0.3)",
            borderRadius: 8,
            padding: "10px 12px",
            color: "var(--danger)",
            fontSize: 12.5,
          }}
        >
          {actionError}
        </div>
      )}

      {run.error ? (
        <InlineError message={run.error} onRetry={run.reload} />
      ) : !run.data ? (
        <Card>
          <Skeleton height={16} width={220} />
          <Skeleton height={12} width={320} style={{ marginTop: 10 }} />
        </Card>
      ) : (
        <>
          {/* Meta row */}
          <Card pad={14}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "10px 28px" }}>
              <Meta label="graph" value={run.data.graph_version_ref} />
              <Meta label="thread" value={run.data.thread_id} />
              <Meta label="cost" value={hasCost ? fmtCost(runCost) : "—"} />
              <Meta label="started" value={startedAt ? fmtDateTime(startedAt) : "—"} />
            </div>
          </Card>

          {/* Approval-hold banner */}
          {run.data.approval_paused_state && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                background: "rgba(252,211,77,0.08)",
                border: "1px solid rgba(252,211,77,0.3)",
                borderRadius: 8,
                padding: "10px 14px",
              }}
            >
              <span style={{ fontSize: 12.5, color: "var(--warning)" }}>
                Held for approval at node{" "}
                <span style={{ fontFamily: "var(--font-mono)" }}>
                  {run.data.approval_paused_state.node_id}
                </span>
                .
              </span>
              <Link
                href="/approvals"
                style={{ fontSize: 12, color: "var(--accent)", textDecoration: "none", flexShrink: 0 }}
              >
                Resolve in Approvals →
              </Link>
            </div>
          )}

          {/* Failure banner — red-tinted frame around the error string */}
          {run.data.failure_state && (
            <div
              style={{
                background: "rgba(248,113,113,0.08)",
                border: "1px solid rgba(248,113,113,0.3)",
                borderRadius: 8,
                padding: 12,
              }}
            >
              <CodeBlock label="Failure" code={failureText(run.data.failure_state)} />
            </div>
          )}

          {/* Terminal output (when present) */}
          {run.data.terminal_output != null && (
            <CodeBlock label="Output" code={jsonText(run.data.terminal_output)} />
          )}

          {/* Node timeline */}
          <Card label="Node timeline" pad={14}>
            <NodeTimeline load={timeline} />
          </Card>

          {/* Evidence + verify */}
          <EvidencePanel runId={runId} />

          {/* Invoke */}
          <Card label="Invoke this deployment" pad={14}>
            <p style={{ margin: "0 0 10px", fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.5 }}>
              The deployed graph is an API service. Call it from anywhere with the run creation
              endpoint (the key is redacted — supply your own via <code>$ZEROTH_API_KEY</code>).
            </p>
            <CodeBlock code={buildInvokeCurl(run.data.thread_id)} />
          </Card>
        </>
      )}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <MonoLabel style={{ display: "block", marginBottom: 3 }}>{label}</MonoLabel>
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 11.5,
          color: "var(--text-secondary)",
          wordBreak: "break-all",
        }}
      >
        {value}
      </span>
    </div>
  );
}

// --------------------------------------------------------------------------
// Node timeline
// --------------------------------------------------------------------------

function NodeTimeline({ load }: { load: Loadable<AuditTimeline> }) {
  if (load.loading && !load.data) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} height={22} />
        ))}
      </div>
    );
  }
  if (load.error) return <InlineError message={load.error} onRetry={load.reload} />;
  const entries = load.data?.entries ?? [];
  if (entries.length === 0)
    return <div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>No nodes recorded yet.</div>;

  return (
    <div>
      {entries.map((e) => (
        <TimelineRow key={e.audit_id} rec={e} />
      ))}
    </div>
  );
}

function TimelineRow({ rec }: { rec: NodeAuditRecord }) {
  const s = rec.status.toLowerCase();
  const tone = NODE_TONE[s] ?? "neutral";
  const running = NODE_RUNNING.has(s);
  const queued = NODE_QUEUED.has(s);
  const type = nodeTypeOf(rec);
  const dur = fmtDuration(rec.started_at, rec.completed_at);
  const note =
    rec.error ??
    (rec.attempt > 1 ? `retry #${rec.attempt}` : "");

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "9px 0",
        borderTop: "1px solid var(--hair)",
        opacity: queued ? 0.5 : 1,
      }}
    >
      <StatusDot tone={tone} pulse={running} />
      <span
        aria-hidden
        style={{
          width: 8,
          height: 8,
          borderRadius: 2,
          flexShrink: 0,
          background: nodeTypeColor(type ?? ""),
        }}
      />
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          color: "var(--text-primary)",
          width: 150,
          flexShrink: 0,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
        title={rec.node_id}
      >
        {rec.node_id}
      </span>
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 9.5,
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          color: "var(--text-muted)",
          width: 90,
          flexShrink: 0,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {type ?? ""}
      </span>
      <span
        style={{
          flex: 1,
          minWidth: 0,
          fontSize: 11.5,
          color: rec.error ? "var(--danger)" : "var(--text-faint)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
        title={note || undefined}
      >
        {running ? "…" : note}
      </span>
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-faint)",
          flexShrink: 0,
          textAlign: "right",
          whiteSpace: "nowrap",
        }}
      >
        {dur ?? ""}
        {rec.cost_usd != null ? `  ${fmtCost(rec.cost_usd)}` : ""}
      </span>
    </div>
  );
}

// --------------------------------------------------------------------------
// Evidence + audit-chain verification
// --------------------------------------------------------------------------

type VerifyState =
  | { phase: "idle" }
  | { phase: "verifying" }
  | { phase: "done"; result: AuditVerification }
  | { phase: "error"; msg: string };

function EvidencePanel({ runId }: { runId: string }) {
  const evidence = useLoad<RunEvidence>(() => getRunEvidence(runId));
  const { markVerified } = useAuditVerification();
  const [verify, setVerify] = useState<VerifyState>({ phase: "idle" });

  async function runVerify() {
    setVerify({ phase: "verifying" });
    try {
      const result = await verifyRunChain(runId);
      setVerify({ phase: "done", result });
      if (result.verified && result.signature_verified !== false) {
        markVerified(new Date().toISOString());
      }
    } catch (e) {
      setVerify({ phase: "error", msg: errMsg(e) });
    }
  }

  return (
    <Card label="Evidence" pad={14}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 10,
        }}
      >
        <VerifyChip state={verify} />
        <Button
          variant="primary"
          disabled={verify.phase === "verifying"}
          onClick={runVerify}
        >
          {verify.phase === "verifying" ? "Verifying…" : "Verify chain"}
        </Button>
      </div>
      {evidence.loading && !evidence.data ? (
        <Skeleton height={80} />
      ) : evidence.error ? (
        <InlineError message={evidence.error} onRetry={evidence.reload} />
      ) : (
        <CodeBlock code={jsonText(evidence.data)} />
      )}
    </Card>
  );
}

function VerifyChip({ state }: { state: VerifyState }) {
  if (state.phase === "idle")
    return <ChipText tone="muted">not verified</ChipText>;
  if (state.phase === "verifying")
    return <ChipText tone="accent">verifying…</ChipText>;
  if (state.phase === "error")
    return <ChipText tone="danger">verify failed</ChipText>;

  const r = state.result;
  const records = `${r.record_count} record${r.record_count === 1 ? "" : "s"}`;
  if (!r.verified)
    return <ChipText tone="danger">chain broken{r.error ? ` · ${r.error}` : ""}</ChipText>;
  if (r.signature_verified === true)
    return <ChipText tone="success">chain intact · signatures valid ({records})</ChipText>;
  if (r.signature_verified === false)
    return <ChipText tone="danger">signature invalid ({records})</ChipText>;
  return <ChipText tone="warning">chain intact · unsigned ({records})</ChipText>;
}

function ChipText({ tone, children }: { tone: string; children: React.ReactNode }) {
  const c = toneColor(tone);
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        color: c,
      }}
    >
      <StatusDot tone={tone} pulse={false} />
      {children}
    </span>
  );
}

// --------------------------------------------------------------------------
// Shared bits
// --------------------------------------------------------------------------

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ padding: 18, fontSize: 12.5, color: "var(--text-muted)" }}>{children}</div>
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

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function failureText(fs: NonNullable<RunStatus["failure_state"]>): string {
  const lines = [`reason: ${fs.reason}`];
  if (fs.message) lines.push(`message: ${fs.message}`);
  if (fs.details && Object.keys(fs.details).length > 0) lines.push(jsonText(fs.details));
  return lines.join("\n");
}

function buildInvokeCurl(threadId: string): string {
  const base = getApiBase() || "https://your-zeroth-host";
  const body = JSON.stringify({ input_payload: { question: "…" }, thread_id: threadId });
  return [
    `curl -X POST ${base}/v1/runs \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -H "X-API-Key: $ZEROTH_API_KEY" \\`,
    `  -d '${body}'`,
  ].join("\n");
}
