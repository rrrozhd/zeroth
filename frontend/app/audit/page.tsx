"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ApiErrorNote,
  Button,
  Empty,
  fmtTime,
  fmtUsd,
  Json,
  NotConnected,
  PageHeader,
  Skeleton,
  StatusBadge,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import {
  type AuditVerification,
  errMsg,
  getDeploymentAuditVerification,
  getDeploymentEvidence,
  getRunEvidence,
  listAudits,
  type NodeAuditRecord,
} from "@/app/lib/api";

// Serialize a bundle to a downloaded JSON file (static-export-safe, client-only).
function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function AuditPage() {
  const connected = useConnected();
  const { data, error, loading, reload } = useAsync(listAudits, []);
  const records = data?.records ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit"
        subtitle="Per-node audit records for this deployment."
        actions={
          <div className="flex items-center gap-2">
            {connected && <ExportEvidenceButton />}
            <Button onClick={() => reload()} disabled={loading}>
              {loading ? "Loading…" : "Refresh"}
            </Button>
          </div>
        }
      />

      {connected && <DeploymentVerifyBadge />}

      {!connected && <NotConnected />}
      {connected && error && <ApiErrorNote error={error} />}
      {connected && loading && !data && <Skeleton rows={5} />}
      {connected && data && records.length === 0 && (
        <Empty>
          No audit records yet. Every node execution writes one —{" "}
          <Link href="/runs" className="font-medium text-accent hover:underline">
            submit a run
          </Link>{" "}
          to see the trail: status, tokens, cost, tool calls, and memory access per node.
        </Empty>
      )}

      <div className="space-y-2">
        {records.map((r) => (
          <AuditRow key={r.audit_id} record={r} />
        ))}
      </div>
    </div>
  );
}

// Deployment-wide tamper-evidence: verifies the digest chain + signatures across
// every run. Same three-state semantics as the run-level badge. Renders nothing
// when unavailable (e.g. the key lacks audit-read).
function DeploymentVerifyBadge() {
  const [result, setResult] = useState<AuditVerification | null>(null);
  useEffect(() => {
    let cancelled = false;
    getDeploymentAuditVerification()
      .then((r) => !cancelled && setResult(r))
      .catch(() => !cancelled && setResult(null));
    return () => {
      cancelled = true;
    };
  }, []);
  if (result === null || result.record_count === 0) return null;
  const label = `${result.record_count} record${
    result.record_count === 1 ? "" : "s"
  } across the deployment`;
  if (!result.verified) {
    return (
      <VerifyPill
        tone="red"
        icon="✕"
        text="Audit chain broken"
        title={`${result.error ?? "Digest chain broken"}${
          result.failed_audit_id ? ` at ${result.failed_audit_id}` : ""
        }`}
      />
    );
  }
  if (result.signature_verified === true) {
    return (
      <VerifyPill
        tone="green"
        icon="✓"
        text="Signed & verified"
        title={`Signed & verified over ${label}${
          result.signing_key_id ? ` under key ${result.signing_key_id}` : ""
        }`}
      />
    );
  }
  if (result.signature_verified === false) {
    return (
      <VerifyPill
        tone="red"
        icon="✕"
        text="Signature invalid / tampered"
        title={`A signed audit record failed signature verification${
          result.failed_audit_id ? ` at ${result.failed_audit_id}` : ""
        }`}
      />
    );
  }
  return (
    <VerifyPill
      tone="amber"
      icon="◇"
      text="Chain intact (unsigned)"
      title={`Digest chain intact over ${label}; records are not cryptographically signed`}
    />
  );
}

function VerifyPill({
  tone,
  icon,
  text,
  title,
}: {
  tone: "green" | "red" | "amber";
  icon: string;
  text: string;
  title: string;
}) {
  const tones = {
    green: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
    red: "bg-red-500/12 text-red-700 dark:text-red-400",
    amber: "bg-amber-500/12 text-amber-700 dark:text-amber-400",
  };
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}
    >
      <span aria-hidden>{icon}</span> {text}
    </span>
  );
}

// Download the deployment's full compliance evidence bundle as JSON.
function ExportEvidenceButton() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    setBusy(true);
    setError(null);
    try {
      const bundle = await getDeploymentEvidence();
      downloadJson(`evidence-deployment-${new Date().toISOString().slice(0, 10)}.json`, bundle);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="flex flex-col items-end gap-1">
      <Button onClick={run} disabled={busy}>
        {busy ? "Exporting…" : "Export evidence"}
      </Button>
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
    </div>
  );
}

function AuditRow({ record }: { record: NodeAuditRecord }) {
  return (
    <details className="group rounded-xl border border-border bg-surface">
      <summary className="flex cursor-pointer items-center justify-between px-4 py-3 [&::-webkit-details-marker]:hidden">
        <span className="flex min-w-0 items-center gap-3">
          <svg
            aria-hidden
            viewBox="0 0 16 16"
            fill="none"
            className="h-3.5 w-3.5 shrink-0 text-muted transition-transform group-open:rotate-90"
          >
            <path
              d="M6 4l4 4-4 4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="font-medium">{record.node_id}</span>
          <span className="truncate font-mono text-xs text-muted">{record.run_id}</span>
        </span>
        <span className="flex shrink-0 items-center gap-3 text-xs text-muted">
          {record.started_at && <span>{fmtTime(record.started_at)}</span>}
          {record.cost_usd != null && <span>{fmtUsd(record.cost_usd)}</span>}
          {record.attempt != null && <span>attempt {record.attempt}</span>}
          <RunEvidenceLink runId={record.run_id} />
          <StatusBadge status={record.status} />
        </span>
      </summary>
      <div className="space-y-3 border-t border-border p-4 text-sm">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
          <Field label="Audit ID" value={record.audit_id} mono />
          <Field label="Started" value={fmtTime(record.started_at)} />
          <Field label="Completed" value={fmtTime(record.completed_at)} />
          {record.token_usage && (
            <Field
              label="Tokens"
              value={`${record.token_usage.input_tokens ?? 0} in / ${record.token_usage.output_tokens ?? 0} out`}
            />
          )}
        </dl>
        {record.error && (
          <div className="text-red-700 dark:text-red-400">{record.error}</div>
        )}
        {(record.tool_calls?.length ?? 0) > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-muted">
              Tool calls ({record.tool_calls!.length})
            </summary>
            <div className="mt-2">
              <Json value={record.tool_calls} />
            </div>
          </details>
        )}
        {(record.memory_interactions?.length ?? 0) > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-muted">
              Memory interactions ({record.memory_interactions!.length})
            </summary>
            <div className="mt-2">
              <Json value={record.memory_interactions} />
            </div>
          </details>
        )}
        {record.output_snapshot != null && (
          <details>
            <summary className="cursor-pointer text-xs text-muted">Output snapshot</summary>
            <div className="mt-2">
              <Json value={record.output_snapshot} />
            </div>
          </details>
        )}
      </div>
    </details>
  );
}

// Per-run evidence export. Sits inside the row's <summary>, so it stops click
// propagation to avoid toggling the disclosure when exporting.
function RunEvidenceLink({ runId }: { runId: string }) {
  const [busy, setBusy] = useState(false);
  async function run(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    setBusy(true);
    try {
      const bundle = await getRunEvidence(runId);
      downloadJson(`evidence-run-${runId}.json`, bundle);
    } catch {
      /* surfaced at the deployment-level export; keep the row quiet */
    } finally {
      setBusy(false);
    }
  }
  return (
    <button
      onClick={run}
      disabled={busy}
      title="Export this run's evidence bundle"
      className="rounded-md px-1.5 py-0.5 font-medium text-muted transition-colors hover:bg-zinc-100 hover:text-foreground dark:hover:bg-zinc-800/60"
    >
      {busy ? "…" : "Evidence"}
    </button>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className={mono ? "font-mono" : ""}>{value}</dd>
    </div>
  );
}
