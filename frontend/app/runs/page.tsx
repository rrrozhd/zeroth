"use client";

// The Runs screen — a master-detail operator view over the live run API.
//
// Left: a filterable list of runs (listRuns). Right: the selected run's detail
// (getRun) with admin actions, a per-node timeline (getRunTimeline), an evidence
// bundle (getRunEvidence), and audit-chain verification (verifyRunChain).
//
// Every read happens client-side and degrades gracefully: an unconfigured or
// unreachable API surfaces as an inline error (with Retry) or an empty state,
// never a crash. Authentication uses an HttpOnly session cookie — it is
// never logged and never placed in a URL; the Invoke cURL block shows a
// redacted `$ZEROTH_API_KEY` placeholder, not the real secret.
//
// NOTE ON DERIVED FIELDS: RunStatusResponse carries no run-level cost or
// timestamps, so the detail's cost/started are summed / min'd from the run's
// NodeAuditRecord timeline entries (cost_usd, started_at) — the only real
// sources. Deployment and graph identities come directly from each run.

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
import { buildRunCurl } from "@/app/components/ui";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import { usePolling } from "@/app/hooks/usePolling";
import {
  ApiError,
  cancelRun,
  errMsg,
  getChildRuns,
  getRun,
  getRunEvidence,
  getRunTimeline,
  getHealth,
  getInputContract,
  interruptRun,
  listRuns,
  replayRun,
  resolveAmbiguousOperation,
  verifyRunChain,
  type AdminRunList,
  type AuditTimeline,
  type AuditVerification,
  type ChildRunSummary,
  type NodeAuditRecord,
  type RunEvidence,
  type RunStatus,
  type OperationResolutionRequest,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";
import { examplePayloadFromSchema } from "@/app/lib/runPayload";
import { TraversalEvidence } from "./TraversalEvidence";

// Statuses that keep the detail (getRun + getRunTimeline) and the list polling
// live — the run is still in flight and its nodes can still advance.
const LIVE = new Set<string>(["running", "queued", "paused_for_approval"]);

// Cancel is offered while running, holding at approval, or paused by an
// interrupt so an operator can cleanly terminate that checkpoint. Interrupt is
// offered only while actively running.
const CANCELLABLE = new Set<string>(["running", "paused_for_approval", "waiting_interrupt"]);

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
const REDACTED_ERROR = "***REDACTED***";

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

function timelineNote(rec: NodeAuditRecord): string {
  if (rec.error && rec.error !== REDACTED_ERROR) return rec.error;
  if (rec.error === REDACTED_ERROR) {
    const meta = rec.execution_metadata as Record<string, unknown> | undefined;
    const reason = meta?.reason_code;
    const readableReason = typeof reason === "string"
      ? reason.replaceAll(/[_-]+/g, " ").trim()
      : "";
    return readableReason ? `${rec.status} · ${readableReason}` : rec.status;
  }
  return rec.attempt > 1 ? `retry #${rec.attempt}` : "";
}

type ContextCompactionEvidence = {
  nodeId: string;
  strategy: string;
  tokensBefore: number;
  tokensAfter: number;
  messagesBefore: number;
  messagesAfter: number;
  threadStateSaved: boolean;
};

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function contextCompactionOf(rec: NodeAuditRecord): ContextCompactionEvidence | null {
  const meta = rec.execution_metadata as Record<string, unknown> | undefined;
  if (!meta || meta.context_compaction_applied !== true) return null;
  const strategy = meta.context_compaction_strategy;
  const tokensBefore = finiteNumber(meta.context_tokens_before);
  const tokensAfter = finiteNumber(meta.context_tokens_after);
  const messagesBefore = finiteNumber(meta.context_messages_before);
  const messagesAfter = finiteNumber(meta.context_messages_after);
  if (
    typeof strategy !== "string"
    || tokensBefore === null
    || tokensAfter === null
    || messagesBefore === null
    || messagesAfter === null
  ) return null;
  return {
    nodeId: rec.node_id,
    strategy,
    tokensBefore,
    tokensAfter,
    messagesBefore,
    messagesAfter,
    threadStateSaved: meta.compacted_thread_state_saved === true,
  };
}

function fmtCost(n: number): string {
  const magnitude = Math.abs(n);
  if (magnitude === 0) return "$0";
  const decimals = magnitude < 0.000001 ? 8 : magnitude < 0.01 ? 6 : magnitude < 1 ? 4 : 2;
  return `$${n.toFixed(decimals).replace(/0+$/, "").replace(/\.$/, "")}`;
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
      <h1 className="sr-only">Runs</h1>
      <RunListPane
        runs={runs}
        connected={connected}
        mounted={mounted}
        filter={filter}
        onFilter={setFilter}
        selected={selected}
        onSelect={select}
      />
      <div
        role="region"
        aria-label="Run details"
        data-evidence-id="runs.region.details"
        tabIndex={0}
        style={{ flex: 1, minWidth: 0, overflowY: "auto" }}
      >
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
      aria-label="Run list"
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
      {/* The filter belongs to the list heading, not to a detached chip cloud. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          padding: "12px 14px",
          borderBottom: "1px solid var(--hair)",
        }}
      >
        <label htmlFor="run-status-filter" style={{ fontSize: 12, fontWeight: 500, color: "var(--text-secondary)" }}>
          Runs
        </label>
        <select
          id="run-status-filter"
          data-evidence-id="runs.filter.status"
          value={filter}
          onChange={(event) => onFilter(event.target.value as FilterId)}
          aria-label="Filter runs by status"
          style={{
            minWidth: 118,
            fontFamily: "var(--font-sans)",
            fontSize: 11.5,
            color: "var(--text-secondary)",
            background: "var(--bg-card)",
            border: "1px solid var(--hair-strong)",
            borderRadius: 8,
            padding: "5px 28px 5px 9px",
          }}
        >
          {FILTERS.map((option) => (
            <option key={option.id} value={option.id}>
              {option.label}
            </option>
          ))}
        </select>
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
      data-evidence-id={`runs.run.${run.run_id}`}
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
        background: selected ? "color-mix(in srgb, var(--accent) 7%, transparent)" : "transparent",
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
        <span title={run.deployment_ref} style={{ color: "var(--text-secondary)" }}>
          {run.deployment_ref}
        </span>
        <span style={{ marginLeft: 6 }}>· {run.graph_version_ref}</span>
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
  const children = useLoad<ChildRunSummary[]>(() => getChildRuns(runId));
  const timeline = useLoad<AuditTimeline>(() => getRunTimeline(runId));
  const attributedEvidence = useLoad<RunEvidence>(() => getRunEvidence(runId));
  const health = useLoad(getHealth);
  const toast = useToast();
  const [busy, setBusy] = useState<null | "cancel" | "interrupt" | "replay">(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [invokePayload, setInvokePayload] = useState<Record<string, unknown>>({});
  const [invokePayloadError, setInvokePayloadError] = useState<string | null>(null);

  const deploymentRef = run.data?.deployment_ref;
  useEffect(() => {
    if (!deploymentRef) return;
    let cancelled = false;
    setInvokePayloadError(null);
    getInputContract(deploymentRef)
      .then((contract) => {
        if (cancelled) return;
        const candidate = examplePayloadFromSchema(contract.json_schema);
        setInvokePayload(
          candidate !== null && typeof candidate === "object" && !Array.isArray(candidate)
            ? (candidate as Record<string, unknown>)
            : {},
        );
      })
      .catch((error) => {
        if (cancelled) return;
        setInvokePayload({});
        setInvokePayloadError(`Input contract unavailable: ${errMsg(error)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [deploymentRef]);

  const status = run.data?.status ?? "";
  const threadId = run.data?.thread_id ?? "";
  const live = LIVE.has(status);

  // While the run is in flight, refetch detail + timeline (~2s) so nodes advance
  // live, and refresh the master list so its row keeps pace.
  usePolling(
    () => {
      run.reload();
      children.reload();
      timeline.reload();
      onListChanged();
    },
    2000,
    live,
  );

  const entries = useMemo(() => timeline.data?.entries ?? [], [timeline.data]);
  const contextCompactions = useMemo(
    () => entries.map(contextCompactionOf).filter((item): item is ContextCompactionEvidence => item !== null),
    [entries],
  );
  const runCost = entries.reduce((a, e) => a + (e.cost_usd ?? 0), 0);
  const hasCost = entries.some((e) => e.cost_usd != null);
  const reconciledCost = attributedEvidence.data?.summary.priced_call_count
    ? attributedEvidence.data.summary.total_cost_usd
    : null;
  const attributedCost = reconciledCost ?? runCost;
  const hasAttributedCost = reconciledCost !== null || hasCost;
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
      children.reload();
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
    setActionError(null);
    try {
      const resp = await replayRun(runId);
      toast(`Requeued ${resp.run_id}`);
      run.reload();
      children.reload();
      timeline.reload();
      onListChanged();
      onSelectRun(resp.run_id);
    } catch (e) {
      const message = `Replay failed: ${errMsg(e)}`;
      setActionError(message);
      toast(message);
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
              data-evidence-id={`runs.action.${runId}.cancel`}
            >
              {busy === "cancel" ? "…" : "Cancel"}
            </Button>
          )}
          {status === "running" && (
            <Button
              variant="neutral"
              disabled={busy !== null}
              onClick={() => act("interrupt", () => interruptRun(runId), "Interrupted")}
              data-evidence-id={`runs.action.${runId}.interrupt`}
            >
              {busy === "interrupt" ? "…" : "Interrupt"}
            </Button>
          )}
          {DANGER_STATES.has(status) && (
            <Button
              variant="neutral"
              disabled={busy !== null}
              onClick={doReplay}
              data-evidence-id={`runs.action.${runId}.replay`}
              title="Requeue this failed run with its original input"
            >
              {busy === "replay" ? "…" : "Replay"}
            </Button>
          )}
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
              <Meta label="deployment" value={run.data.deployment_ref} />
              <Meta label="graph" value={run.data.graph_version_ref} />
              {run.data.thread_id !== run.data.run_id && (
                <Meta label="thread" value={run.data.thread_id} />
              )}
              <Meta
                label="attributed cost"
                value={hasAttributedCost ? fmtCost(attributedCost) : "—"}
              />
              <Meta label="started" value={startedAt ? fmtDateTime(startedAt) : "—"} />
            </div>
          </Card>

          {contextCompactions.length > 0 && (
            <div data-evidence-id="runs.context-window">
              <Card label="Context management" pad={14}>
                <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
                {contextCompactions.map((context, index) => (
                  <div
                    key={`${context.nodeId}-${index}`}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: "6px 14px",
                      paddingTop: index === 0 ? 0 : 9,
                      borderTop: index === 0 ? "none" : "1px solid var(--hair)",
                    }}
                  >
                    <MonoLabel>{context.nodeId}</MonoLabel>
                    <Pill tone="info">{context.strategy.replaceAll("_", " ")}</Pill>
                    <span style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                      {context.tokensBefore} → {context.tokensAfter} tokens
                    </span>
                    <span style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
                      {context.messagesBefore} → {context.messagesAfter} messages
                    </span>
                    <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
                      1 compaction · {context.threadStateSaved
                        ? `state saved to thread ${threadId}`
                        : "thread state not saved"}
                    </span>
                  </div>
                ))}
                </div>
              </Card>
            </div>
          )}

          {(run.data.parent_run_id || children.loading || children.error || (children.data?.length ?? 0) > 0) && (
            <Card pad={14}>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", marginBottom: 10 }}>
                Composed lineage
              </div>
              {run.data.parent_run_id && (
                <div data-evidence-id="runs.lineage.parent" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
                  <span style={{ fontSize: 11, color: "var(--text-faint)" }}>Parent run</span>
                  <button
                    type="button"
                    onClick={() => onSelectRun(run.data!.parent_run_id!)}
                    style={{ border: 0, padding: 0, background: "transparent", color: "var(--accent)", fontFamily: "var(--font-mono)", cursor: "pointer" }}
                  >
                    {run.data.parent_run_id}
                  </button>
                </div>
              )}
              {children.loading && !children.data ? (
                <Skeleton height={28} />
              ) : children.error ? (
                <InlineError message={children.error} onRetry={children.reload} />
              ) : (children.data?.length ?? 0) > 0 ? (
                <div data-evidence-id="runs.lineage.children" style={{ display: "flex", flexDirection: "column", gap: 7 }}>
                  <span style={{ fontSize: 11, color: "var(--text-faint)" }}>Child runs ({children.data!.length})</span>
                  {children.data!.map((child) => (
                    <button
                      key={child.run_id}
                      type="button"
                      onClick={() => onSelectRun(child.run_id)}
                      data-evidence-id={`runs.lineage.child.${child.run_id}`}
                      style={{ display: "flex", alignItems: "center", gap: 9, border: "1px solid var(--hair)", borderRadius: 7, padding: "7px 9px", background: "var(--bg-chrome)", color: "inherit", cursor: "pointer", textAlign: "left" }}
                    >
                      <StatusDot tone={runTone(child.status)} />
                      <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--accent)" }}>{child.run_id}</span>
                      <span style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--text-faint)" }}>{child.thread_id}</span>
                    </button>
                  ))}
                </div>
              ) : null}
            </Card>
          )}

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

          <TraversalEvidence traversal={run.data.traversal} />

          {/* Node timeline */}
          <Card label="Node timeline" pad={14}>
            <NodeTimeline load={timeline} />
          </Card>

          {/* Evidence + verify */}
          <EvidencePanel runId={runId} evidence={attributedEvidence} />

          {/* Invoke */}
          <Card label="Invoke this deployment" pad={14}>
            <p style={{ margin: "0 0 10px", fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.5 }}>
              The deployed graph is an API service. Call it from anywhere with the run creation
              endpoint (the key is redacted — supply your own via <code>$ZEROTH_API_KEY</code>).
            </p>
            {invokePayloadError && (
              <p role="alert" style={{ margin: "0 0 10px", color: "var(--warning)", fontSize: 11.5 }}>
                {invokePayloadError}; the example uses an empty object.
              </p>
            )}
            <CodeBlock
              code={buildRunCurl(
                JSON.stringify(invokePayload),
                run.data.thread_id,
                health.data?.campaign_id,
              )}
            />
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
  const note = timelineNote(rec);

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

function EvidencePanel({ runId, evidence }: { runId: string; evidence: Loadable<RunEvidence> }) {
  const { markVerified } = useAuditVerification();
  const [verify, setVerify] = useState<VerifyState>({ phase: "idle" });
  const [showRaw, setShowRaw] = useState(false);

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
          data-evidence-id={`runs.evidence.${runId}.verify-chain`}
        >
          {verify.phase === "verifying" ? "Verifying…" : "Verify chain"}
        </Button>
      </div>
      {evidence.loading && !evidence.data ? (
        <Skeleton height={80} />
      ) : evidence.error ? (
        <InlineError message={evidence.error} onRetry={evidence.reload} />
      ) : evidence.data ? (
        <>
          <AmbiguousOperationResolutions
            evidence={evidence.data}
            onResolved={evidence.reload}
          />
          <EvidenceSummary evidence={evidence.data} />
          <div style={{ marginTop: 12 }}>
            <Button
              variant="neutral"
              onClick={() => setShowRaw((visible) => !visible)}
              data-evidence-id={`runs.evidence.${runId}.toggle-raw`}
            >
              {showRaw ? "Hide raw evidence" : "Show raw evidence"}
            </Button>
          </div>
          {showRaw ? (
            <CodeBlock
              label="Metadata-only evidence JSON"
              code={jsonText(evidence.data)}
              style={{ marginTop: 10 }}
            />
          ) : null}
        </>
      ) : (
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 12.5 }}>
          No evidence is available for this run.
        </p>
      )}
    </Card>
  );
}

type AmbiguousOperation = {
  operationKey: string;
  alias: string;
  toolRef: string;
};

function ambiguousOperationsOf(evidence: RunEvidence): AmbiguousOperation[] {
  const resolved = new Set<string>();
  const ambiguous = new Map<string, AmbiguousOperation>();
  for (const audit of evidence.audits ?? []) {
    const metadata = audit.execution_metadata as Record<string, unknown> | undefined;
    if (
      audit.node_id === "operation.resolve"
      && typeof metadata?.operation_key === "string"
      && ["completed", "failed"].includes(String(metadata.operation_state).toLowerCase())
    ) {
      resolved.add(metadata.operation_key);
    }
    for (const call of audit.tool_calls ?? []) {
      if (
        typeof call.operation_key === "string"
        && call.operation_key
        && call.operation_state?.toUpperCase() === "AMBIGUOUS"
      ) {
        ambiguous.set(call.operation_key, {
          operationKey: call.operation_key,
          alias: call.alias,
          toolRef: call.tool_ref,
        });
      }
    }
  }
  return [...ambiguous.values()].filter((operation) => !resolved.has(operation.operationKey));
}

function AmbiguousOperationResolutions({
  evidence,
  onResolved,
}: {
  evidence: RunEvidence;
  onResolved: () => void;
}) {
  const operations = ambiguousOperationsOf(evidence);
  if (operations.length === 0) return null;
  return (
    <div style={{ display: "grid", gap: 10, marginBottom: 14 }}>
      {operations.map((operation) => (
        <OperationResolutionForm
          key={operation.operationKey}
          deploymentRef={evidence.run.deployment_ref}
          operation={operation}
          onResolved={onResolved}
        />
      ))}
    </div>
  );
}

function operationResolutionError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return "The active role does not have permission to resolve ambiguous operations.";
    }
    if (error.status === 409) {
      return "This operation is no longer ambiguous. Refresh the evidence before acting again.";
    }
    if (error.status === 503) {
      return "Resolution is temporarily unavailable because its signed audit or operation store is unavailable.";
    }
  }
  return `Resolution failed: ${errMsg(error)}`;
}

function OperationResolutionForm({
  deploymentRef,
  operation,
  onResolved,
}: {
  deploymentRef: string;
  operation: AmbiguousOperation;
  onResolved: () => void;
}) {
  const evidenceBase = `runs.evidence.operation-resolution.${operation.operationKey}`;
  const toast = useToast();
  const [resolution, setResolution] = useState<OperationResolutionRequest["resolution"]>("completed");
  const [reason, setReason] = useState("");
  const [receipt, setReceipt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedReason = reason.trim();
    if (!trimmedReason) return;
    let parsedReceipt: unknown;
    if (receipt.trim()) {
      try {
        parsedReceipt = JSON.parse(receipt);
      } catch {
        setError("Receipt must be valid JSON.");
        return;
      }
    }
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const body: OperationResolutionRequest = {
        resolution,
        reason: trimmedReason,
        ...(receipt.trim() ? { receipt: parsedReceipt } : {}),
      };
      const result = await resolveAmbiguousOperation(
        deploymentRef,
        operation.operationKey,
        body,
      );
      const message = `Operation recorded as ${result.state.toLowerCase()}. Run state was not changed.`;
      setDone(message);
      toast(message);
      onResolved();
    } catch (caught) {
      setError(operationResolutionError(caught));
    } finally {
      setBusy(false);
    }
  }

  const controlStyle: React.CSSProperties = {
    width: "100%",
    boxSizing: "border-box",
    color: "var(--text-primary)",
    background: "var(--bg-card)",
    border: "1px solid var(--hair-strong)",
    borderRadius: 7,
    padding: "7px 9px",
    fontFamily: "var(--font-mono)",
    fontSize: 11.5,
  };

  return (
    <form
      onSubmit={submit}
      data-evidence-id={evidenceBase}
      style={{
        padding: 12,
        border: "1px solid color-mix(in srgb, var(--warning) 45%, var(--hair))",
        borderRadius: 9,
        background: "color-mix(in srgb, var(--warning) 5%, var(--bg-raised))",
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
        <Pill tone="warning">ambiguous operation</Pill>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
          {operation.operationKey}
        </span>
        <span style={{ color: "var(--text-faint)", fontSize: 11.5 }}>
          {operation.alias} · {operation.toolRef}
        </span>
      </div>
      <p style={{ margin: "8px 0 10px", color: "var(--text-secondary)", fontSize: 11.5, lineHeight: 1.5 }}>
        Record the provider-authoritative outcome. This changes only the durable operation record and
        does not resume or replay the run.
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "160px minmax(220px, 1fr)", gap: 10 }}>
        <label style={{ color: "var(--text-secondary)", fontSize: 11.5 }}>
          Outcome
          <select
            value={resolution}
            onChange={(event) => setResolution(event.target.value as OperationResolutionRequest["resolution"])}
            data-evidence-id={`${evidenceBase}.outcome`}
            style={{ ...controlStyle, marginTop: 4 }}
          >
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
          </select>
        </label>
        <label style={{ color: "var(--text-secondary)", fontSize: 11.5 }}>
          Reason <span aria-hidden="true">*</span>
          <textarea
            required
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            data-evidence-id={`${evidenceBase}.reason`}
            rows={2}
            placeholder="What authoritative evidence determined this outcome?"
            style={{ ...controlStyle, marginTop: 4, resize: "vertical" }}
          />
        </label>
      </div>
      <label style={{ display: "block", marginTop: 10, color: "var(--text-secondary)", fontSize: 11.5 }}>
        Receipt JSON <span style={{ color: "var(--text-faint)" }}>(optional)</span>
        <textarea
          value={receipt}
          onChange={(event) => setReceipt(event.target.value)}
          data-evidence-id={`${evidenceBase}.receipt`}
          rows={2}
          placeholder={'{"provider_reference":"…"}'}
          style={{ ...controlStyle, marginTop: 4, resize: "vertical" }}
        />
      </label>
      {error && (
        <p
          role="alert"
          data-evidence-id={`${evidenceBase}.error`}
          style={{ margin: "8px 0 0", color: "var(--danger)", fontSize: 11.5 }}
        >
          {error}
        </p>
      )}
      {done && (
        <p
          role="status"
          data-evidence-id={`${evidenceBase}.status`}
          style={{ margin: "8px 0 0", color: "var(--success)", fontSize: 11.5 }}
        >
          {done}
        </p>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 10 }}>
        <Button
          type="submit"
          variant="primary"
          disabled={busy || !reason.trim()}
          data-evidence-id={`${evidenceBase}.submit`}
        >
          {busy ? "Recording…" : "Record resolution"}
        </Button>
      </div>
    </form>
  );
}

function EvidenceSummary({ evidence }: { evidence: RunEvidence }) {
  const { summary } = evidence;
  const counts = [
    `${summary.audit_count} audit record${summary.audit_count === 1 ? "" : "s"}`,
    `${summary.approval_count} approval${summary.approval_count === 1 ? "" : "s"}`,
    `${summary.tool_call_count} tool call${summary.tool_call_count === 1 ? "" : "s"}`,
    `${summary.memory_interaction_count} memory event${summary.memory_interaction_count === 1 ? "" : "s"}`,
  ];
  const zeroActivity = summary.cost_identity_state === "not_applicable_no_priced_call";
  const costIdentity = zeroActivity
    ? "Cost identity not applicable"
    : summary.cost_identity_state === "correlated"
      ? "Cost identities correlated"
      : "Cost identity incomplete";
  const reconciliation = summary.reconciliation_state.startsWith("reconciled")
    ? "reconciled"
    : "needs reconciliation";

  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {counts.map((count) => (
          <Pill key={count} tone="neutral">
            {count}
          </Pill>
        ))}
      </div>
      <div
        data-evidence-id="runs.evidence.cost-summary"
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "6px 12px",
          marginTop: 10,
          padding: "9px 10px",
          border: "1px solid var(--hair)",
          borderRadius: 8,
          background: "var(--bg-raised)",
          color: "var(--text-secondary)",
          fontSize: 12,
        }}
      >
        <span>{zeroActivity ? "No priced calls" : `${summary.priced_call_count} priced calls`}</span>
        <span>{costIdentity}</span>
        <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)" }}>
          {fmtCost(summary.total_cost_usd)} {reconciliation}
        </span>
      </div>
      <p style={{ margin: "10px 0 0", color: "var(--text-secondary)", fontSize: 12.5, lineHeight: 1.5 }}>
        Payload values are intentionally withheld under metadata-only capture. Correlation IDs, costs,
        hashes, and signatures remain available for inspection.
      </p>
      {(evidence.audits?.length ?? 0) > 0 ? (
        <div style={{ marginTop: 10, borderTop: "1px solid var(--hair)" }}>
          {evidence.audits?.map((audit) => (
            <div
              key={audit.audit_id}
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(110px, 1fr) 90px 78px minmax(110px, 1fr)",
                gap: 10,
                alignItems: "center",
                padding: "8px 0",
                borderBottom: "1px solid var(--hair)",
                fontSize: 12,
              }}
            >
              <MonoLabel title={audit.node_id}>{audit.node_id}</MonoLabel>
              <span style={{ color: audit.status === "succeeded" ? "var(--success)" : "var(--text-secondary)" }}>
                {audit.status}
              </span>
              <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
                {audit.cost_usd == null ? "—" : fmtCost(audit.cost_usd)}
              </span>
              <span
                title={audit.record_digest ?? "No digest"}
                style={{ fontFamily: "var(--font-mono)", color: "var(--text-faint)", overflow: "hidden", textOverflow: "ellipsis" }}
              >
                {audit.record_digest ? `digest ${audit.record_digest.slice(0, 12)}…` : "digest unavailable"}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
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
