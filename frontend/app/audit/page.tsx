"use client";

// The Audit screen — the tenant's tamper-evident audit trail (handoff §5).
//
// A right-aligned chip runs the chain-verification state machine
// (idle → verifying → intact / unsigned / failed); a successful verify greens
// the `sig` column and completes the Overview checklist for this browser session. Rows
// are colored by event kind derived from each record's real status/error/flags.
// Reads are client-side and degrade to inline error / empty states.

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Button,
  ConsoleEmpty,
  ConsoleMeta,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  ConsoleTableFrame,
  Skeleton,
} from "@/app/components/primitives";
import { useAuditVerification } from "@/app/components/auditVerificationContext";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  deploymentRef,
  errMsg,
  getAuditReadiness,
  listAudits,
  verifyDeploymentAuditChain,
  type TenantAuditRecordList,
  type AuditVerification,
  type AuditReadiness,
  type NodeAuditRecord,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";
import { isForbiddenSurface, surfaceAccessMessage } from "@/app/lib/surfaceAccess";
import styles from "./audit.module.css";


/** HH:MM:SS (24h) or "—". */
function fmtClock(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false });
}

/** HH:MM UTC for the "last verified …" idle chip. */
function fmtUtcHM(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm} UTC`;
}

/** `sha256:9f41c2…b8d0` — prefixed, truncated in the middle. */
function shortDigest(d?: string | null): string {
  if (!d) return "—";
  const body = d.startsWith("sha256:") ? d.slice(7) : d;
  if (body.length <= 12) return `sha256:${body}`;
  return `sha256:${body.slice(0, 6)}…${body.slice(-4)}`;
}

type EventKind = "ok" | "warn" | "denied";
type AuditView = "all" | "workflow" | "security";
const REDACTED_ERROR = "***REDACTED***";

/** Service authentication and authorization checks are persisted in the same
 * audit chain as workflow-node execution. Keep that distinction presentational:
 * verification and storage continue to operate over the complete chain. */
function isSecurityRecord(record: NodeAuditRecord): boolean {
  const nodeId = record.node_id.toLowerCase();
  return nodeId === "service.auth"
    || nodeId.startsWith("service.auth.")
    || nodeId === "service.authorization"
    || nodeId.startsWith("service.authorization.");
}

/** Classify a record for row color. Hard failures/denials (failed, rejected,
 *  or any error) read danger; redaction commitments, erasure, and approval
 *  actions read warn; everything else (completed) reads ok. */
function classifyEvent(r: NodeAuditRecord): EventKind {
  const status = (r.status ?? "").toLowerCase();
  if (r.error || status === "failed" || status === "rejected" || status === "denied" || status === "error") {
    return "denied";
  }
  const hasPii = r.pii_commitments != null && Object.keys(r.pii_commitments).length > 0;
  if (hasPii || r.erased || (r.approval_actions?.length ?? 0) > 0) return "warn";
  return "ok";
}

/** A human event string built only from real fields. */
function eventText(r: NodeAuditRecord): string {
  if (r.error && r.error !== REDACTED_ERROR) return r.error;
  const parts: string[] = [r.status || "record"];
  if ((r.approval_actions?.length ?? 0) > 0) {
    parts.push(`· approval ${r.approval_actions!.map((a) => a.action).join(", ")}`);
  }
  if (r.erased) parts.push(`· erased${r.erasure_reason ? ` (${r.erasure_reason})` : ""}`);
  return parts.join(" ");
}

const EVENT_COLOR: Record<EventKind, string> = {
  ok: "var(--text-secondary)",
  warn: "var(--warning)",
  denied: "var(--danger)",
};

type VerifyPhase = "idle" | "verifying" | "intact" | "unsigned" | "failed";

export default function AuditPage() {
  const load = useLoad<TenantAuditRecordList>(listAudits);
  const readiness = useLoad<AuditReadiness>(getAuditReadiness);
  const toast = useToast();
  const { verifiedAt, markVerified } = useAuditVerification();

  const [connected, setConnected] = useState(false);
  useEffect(() => {
    setConnected(isConfigured());
  }, []);

  const [phase, setPhase] = useState<VerifyPhase>("idle");
  const [result, setResult] = useState<AuditVerification | null>(null);
  const [view, setView] = useState<AuditView>("workflow");

  // Tenant-wide results span many independent run/deployment chains, so their
  // chain-local sequence numbers are not globally sortable. Present the newest
  // activity first; retain sequence only as the per-chain forensic coordinate.
  const records = [...(load.data?.records ?? [])].sort((a, b) => {
    const timeA = Date.parse(a.started_at ?? "");
    const timeB = Date.parse(b.started_at ?? "");
    const byTime = (Number.isNaN(timeB) ? 0 : timeB) - (Number.isNaN(timeA) ? 0 : timeA);
    if (byTime !== 0) return byTime;
    return (b.chain_sequence ?? -1) - (a.chain_sequence ?? -1);
  });
  const securityCount = records.filter(isSecurityRecord).length;
  const workflowCount = records.length - securityCount;
  const visibleRecords = records.filter((record) => (
    view === "all" || (view === "security" ? isSecurityRecord(record) : !isSecurityRecord(record))
  ));
  const sigGreen = phase === "intact";

  async function verify() {
    setPhase("verifying");
    setResult(null);
    try {
      const ref = await deploymentRef();
      const res = await verifyDeploymentAuditChain(ref);
      setResult(res);
      if (res.verified && res.signature_verified === false) {
        setPhase("failed");
        toast("Chain digests intact, but a signature failed verification");
        return;
      }
      if (!res.verified) {
        setPhase("failed");
        toast("Audit chain verification failed");
        return;
      }
      // verified === true (signature true or unsigned-legacy null)
      const signed = res.signature_verified === true;
      setPhase(signed ? "intact" : "unsigned");
      const now = new Date().toISOString();
      markVerified(now);
      toast(signed ? "Chain intact · signatures valid" : "Chain intact · unsigned (legacy)");
    } catch (e) {
      setPhase("failed");
      toast(`Verification failed: ${errMsg(e)}`);
    }
  }

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Audit"
        description="Workflow execution and service security records across this tenant."
        actions={connected ? (
          <>
            <ChainChip phase={phase} result={result} verifiedAt={verifiedAt} />
            <Button
              variant="primary"
              onClick={verify}
              disabled={phase === "verifying"}
              data-evidence-id="audit.verify-chain"
            >
              {phase === "verifying" ? "Verifying…" : "Verify chain"}
            </Button>
          </>
        ) : undefined}
      />

      <div className={styles.contentStack}>
        {connected && readiness.data && (
          <ConsoleNotice
            tone={readiness.data.state === "signed" ? "success" : readiness.data.state === "blocked_unsigned" ? "danger" : "neutral"}
            title={readiness.data.state === "signed"
              ? "Signing configured"
              : readiness.data.state === "blocked_unsigned"
                ? "Deployment blocked — unsigned"
                : "Local only — unsigned audit"}
            actions={<ConsoleMeta>{readiness.data.deployment_mode}</ConsoleMeta>}
          >
            {readiness.data.message}
          </ConsoleNotice>
        )}

        {!connected ? (
          <ConnectNote />
        ) : load.loading && !load.data ? (
          <ConsoleSurface>
            <div className={styles.loading}>
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} height={18} />
              ))}
            </div>
          </ConsoleSurface>
        ) : load.error ? (
          <ErrorNote message={load.error} onRetry={load.reload} />
        ) : records.length === 0 ? (
          <ConsoleEmpty>
            No audit records yet. Every node execution appends one to the chain.{" "}
            <Link href="/runs" className={styles.link}>Submit a run</Link> to populate the trail.
          </ConsoleEmpty>
        ) : (
          <ConsoleSection
            title="Audit records"
            meta={view === "all"
              ? `${records.length.toLocaleString()} records`
              : `${visibleRecords.length.toLocaleString()} of ${records.length.toLocaleString()} records`}
          >
            <ConsoleTableFrame ariaLabel="Audit records">
              <div className={styles.auditToolbar}>
                <p className={styles.viewHelp}>
                  Workflow includes execution evidence. Security includes service authentication and authorization decisions. Payload values are withheld; correlation IDs, digests, signatures, status, and timing remain reviewable. Verify chain checks the actively served deployment; use each run’s Evidence panel for cross-deployment verification.
                </p>
                <div className={styles.viewGroup} role="group" aria-label="Audit record view">
                  {([
                    ["all", "All", records.length],
                    ["workflow", "Workflow", workflowCount],
                    ["security", "Security", securityCount],
                  ] as const).map(([id, label, count]) => (
                    <Button
                      key={id}
                      variant={view === id ? "primary" : "neutral"}
                      aria-pressed={view === id}
                      aria-label={`${label}, ${count.toLocaleString()} records`}
                      onClick={() => setView(id)}
                    >
                      {label}
                    </Button>
                  ))}
                </div>
              </div>
              {visibleRecords.length === 0 ? (
                <p className={styles.filteredEmpty}>No {view} records yet.</p>
              ) : <div className={styles.table} role="table" aria-label={`${view} audit records`}>
                {/* Header row */}
                <div className={styles.tableHeader} role="row">
                  {["chain #", "time", "run", "node", "event", "digest", "sig"].map((h) => (
                    <span key={h} role="columnheader">{h}</span>
                  ))}
                </div>
                {/* Body */}
                {visibleRecords.map((r) => {
                  const kind = classifyEvent(r);
                  return (
                    <div
                      key={r.audit_id}
                      role="row"
                      className={`${styles.tableRow} ${kind === "denied" ? styles.tableRowDenied : ""}`}
                    >
                      <Cell color="var(--text-faint)">{r.chain_sequence ?? "—"}</Cell>
                      <Cell color="var(--text-muted)">{fmtClock(r.started_at)}</Cell>
                      <Cell color="var(--text-muted)" title={r.run_id}>
                        {r.run_id}
                      </Cell>
                      <Cell color="var(--text-secondary)" title={r.node_id}>
                        {r.node_id}
                      </Cell>
                      <Cell color={EVENT_COLOR[kind]} title={eventText(r)}>
                        {eventText(r)}
                      </Cell>
                      <Cell color="var(--text-faint)" title={r.record_digest ?? undefined}>
                        {shortDigest(r.record_digest)}
                      </Cell>
                      <div
                        role="cell"
                        className={styles.signature}
                        style={{ color: r.record_signature ? sigGreen ? "var(--success)" : "var(--text-faint)" : "var(--text-secondary)" }}
                        title={r.record_signature ? "signed" : "unsigned"}
                      >
                        {r.record_signature ? "yes" : "no"}
                      </div>
                    </div>
                  );
                })}
              </div>}
            </ConsoleTableFrame>
            <p className={styles.tableNote}>
              Erasure is chain-safe: crypto-erasure removes payloads while the digest chain stays
              continuous, so the audit trail remains verifiable after a right-to-erasure request.
            </p>
          </ConsoleSection>
        )}
      </div>
    </ConsolePage>
  );
}

function Cell({
  color,
  title,
  children,
}: {
  color: string;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="cell"
      className={styles.cell}
      title={title}
      style={{
        color,
      }}
    >
      {children}
    </div>
  );
}

function ChainChip({
  phase,
  result,
  verifiedAt,
}: {
  phase: VerifyPhase;
  result: AuditVerification | null;
  verifiedAt: string | null;
}) {
  let tone = "neutral";
  let text: string;
  switch (phase) {
    case "verifying":
      tone = "accent";
      text = "verifying active deployment…";
      break;
    case "intact":
      tone = "success";
      text = "chain intact · signatures valid";
      break;
    case "unsigned":
      text = "chain intact · unsigned";
      break;
    case "failed":
      tone = "danger";
      text = result?.failed_audit_id
        ? `chain broken at ${result.failed_audit_id}${result.error ? ` · ${result.error}` : ""}`
        : result?.error
          ? `chain broken · ${result.error}`
          : "chain verification failed";
      break;
    default:
      text = verifiedAt ? `last verified ${fmtUtcHM(verifiedAt)}` : "Chain not verified this session";
  }
  return (
    <span
      className={styles.chainState}
      data-tone={tone}
      data-evidence-id="audit.verify-chain.result"
      role="status"
      aria-live="polite"
    >
      {text}
    </span>
  );
}

function ConnectNote() {
  return (
    <ConsoleNotice title="Not connected">
      Open Connect from the navigation to set the API base and key.
    </ConsoleNotice>
  );
}

function ErrorNote({ message, onRetry }: { message: string; onRetry: () => void }) {
  const restricted = isForbiddenSurface(message);
  return (
    <ConsoleNotice
      tone={restricted ? "neutral" : "danger"}
      title={restricted ? "Access restricted" : "Audit unavailable"}
      actions={restricted ? undefined : <Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {surfaceAccessMessage(message, "Audit records")}
    </ConsoleNotice>
  );
}
