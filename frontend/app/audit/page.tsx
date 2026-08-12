"use client";

// The Audit screen — the deployment's tamper-evident audit trail (handoff §5).
//
// A right-aligned chip runs the chain-verification state machine
// (idle → verifying → intact / unsigned / failed); a successful verify greens
// the `sig` column and completes the Overview checklist for this browser session. Rows
// are colored by event kind derived from each record's real status/error/flags.
// Reads are client-side and degrade to inline error / empty states.

import { useEffect, useState } from "react";
import Link from "next/link";
import { Button, Card, MonoLabel, Skeleton } from "@/app/components/primitives";
import { useAuditVerification } from "@/app/components/auditVerificationContext";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  deploymentRef,
  errMsg,
  listAudits,
  verifyDeploymentAuditChain,
  type AuditRecordList,
  type AuditVerification,
  type NodeAuditRecord,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

const MONO = "var(--font-mono)";
const COLS = "48px 66px 110px 110px minmax(160px,1fr) 170px 40px";
const DENIED_BG = "rgba(248,113,113,0.06)";

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
  if (r.error) return r.error;
  const parts: string[] = [r.status || "record"];
  if ((r.approval_actions?.length ?? 0) > 0) {
    parts.push(`· approval ${r.approval_actions!.map((a) => a.action).join(", ")}`);
  }
  if (r.pii_commitments != null && Object.keys(r.pii_commitments).length > 0) parts.push("· redacted");
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
  const load = useLoad<AuditRecordList>(listAudits);
  const toast = useToast();
  const { verifiedAt, markVerified } = useAuditVerification();

  const [connected, setConnected] = useState(false);
  useEffect(() => {
    setConnected(isConfigured());
  }, []);

  const [phase, setPhase] = useState<VerifyPhase>("idle");
  const [result, setResult] = useState<AuditVerification | null>(null);

  const records = [...(load.data?.records ?? [])].sort(
    (a, b) => (a.chain_sequence ?? Number.MAX_SAFE_INTEGER) - (b.chain_sequence ?? Number.MAX_SAFE_INTEGER),
  );
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
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header
        style={{ display: "flex", alignItems: "flex-start", gap: 16, marginBottom: 22 }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Audit</h1>
          <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
            Tamper-evident, per-node audit records for this deployment.
          </p>
        </div>
        {connected && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
            <ChainChip phase={phase} result={result} recordCount={records.length} verifiedAt={verifiedAt} />
            <Button variant="primary" onClick={verify} disabled={phase === "verifying"}>
              {phase === "verifying" ? "Verifying…" : "Verify chain"}
            </Button>
          </div>
        )}
      </header>

      {!connected ? (
        <ConnectNote />
      ) : load.loading && !load.data ? (
        <Card pad={16}>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} height={18} />
            ))}
          </div>
        </Card>
      ) : load.error ? (
        <ErrorNote message={load.error} onRetry={load.reload} />
      ) : records.length === 0 ? (
        <Card pad={20}>
          <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
            No audit records yet. Every node execution appends one to the chain —{" "}
            <Link href="/runs" style={{ color: "var(--accent)", textDecoration: "none" }}>
              submit a run
            </Link>{" "}
            to populate the trail.
          </div>
        </Card>
      ) : (
        <>
          <Card pad={0} style={{ overflow: "hidden" }}>
            <div style={{ overflowX: "auto" }}>
              <div style={{ minWidth: 720 }}>
                {/* Header row */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: COLS,
                    gap: 12,
                    padding: "10px 16px",
                    borderBottom: "1px solid var(--hair)",
                  }}
                >
                  {["seq", "time", "run", "node", "event", "digest", "sig"].map((h) => (
                    <MonoLabel key={h} style={{ fontSize: 10 }}>
                      {h}
                    </MonoLabel>
                  ))}
                </div>
                {/* Body */}
                {records.map((r) => {
                  const kind = classifyEvent(r);
                  return (
                    <div
                      key={r.audit_id}
                      style={{
                        display: "grid",
                        gridTemplateColumns: COLS,
                        gap: 12,
                        padding: "9px 16px",
                        borderBottom: "1px solid var(--hair)",
                        background: kind === "denied" ? DENIED_BG : "transparent",
                        fontFamily: MONO,
                        fontSize: 11.5,
                        alignItems: "center",
                      }}
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
                        style={{
                          color: r.record_signature
                            ? sigGreen
                              ? "var(--success)"
                              : "var(--text-faint)"
                            : "var(--text-disabled)",
                          textAlign: "center",
                        }}
                        title={r.record_signature ? "signed" : "unsigned"}
                      >
                        {r.record_signature ? "✓" : "—"}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </Card>
          <p style={{ marginTop: 12, fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.6 }}>
            Erasure is chain-safe: crypto-erasure removes payloads while the digest chain stays
            continuous, so the audit trail remains verifiable after a right-to-erasure request.
          </p>
        </>
      )}
    </div>
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
      title={title}
      style={{
        color,
        overflow: "hidden",
        textOverflow: "ellipsis",
        whiteSpace: "nowrap",
        minWidth: 0,
      }}
    >
      {children}
    </div>
  );
}

function ChainChip({
  phase,
  result,
  recordCount,
  verifiedAt,
}: {
  phase: VerifyPhase;
  result: AuditVerification | null;
  recordCount: number;
  verifiedAt: string | null;
}) {
  let color = "var(--text-faint)";
  let text: string;
  switch (phase) {
    case "verifying":
      color = "var(--accent)";
      text = `verifying ${recordCount.toLocaleString()} records…`;
      break;
    case "intact":
      color = "var(--success)";
      text = "chain intact · signatures valid";
      break;
    case "unsigned":
      color = "var(--neutral)";
      text = "chain intact · unsigned";
      break;
    case "failed":
      color = "var(--danger)";
      text = result?.failed_audit_id
        ? `chain broken at ${result.failed_audit_id.slice(0, 8)}`
        : "chain verification failed";
      break;
    default:
      text = verifiedAt ? `last verified ${fmtUtcHM(verifiedAt)}` : "not yet verified";
  }
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        color,
        background: `color-mix(in srgb, ${color} 10%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 28%, transparent)`,
        borderRadius: 6,
        padding: "4px 9px",
        fontFamily: MONO,
        fontSize: 11,
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

function ConnectNote() {
  return (
    <Card pad={20}>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Not connected. Open <span style={{ color: "var(--accent)" }}>Connect</span> (bottom-left) to
        set the API base and key.
      </div>
    </Card>
  );
}

function ErrorNote({ message, onRetry }: { message: string; onRetry: () => void }) {
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
        padding: "12px 14px",
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
