"use client";

// The Approvals screen — human-approval gates waiting on a reviewer.
//
// One card per approval (handoff README §4). Reads happen client-side via
// `useLoad` and degrade to inline error / empty states, never a thrown boundary.
// Resolving flips the card optimistically, toasts, and refetches so the sidebar
// badge and the run both catch up. Authentication uses an HttpOnly cookie — it
// is never logged and never placed in a URL.

import { useEffect, useState } from "react";
import Link from "next/link";
import { Button, Card, CodeBlock, MonoLabel, Pill, Skeleton } from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  errMsg,
  listApprovals,
  resolveApproval,
  type ApprovalRecord,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

const MONO = "var(--font-mono)";

/** HH:MM:SS in the viewer's locale (24h), or "—" for a missing/bad timestamp. */
function fmtClock(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false });
}

type Decision = "approve" | "reject";

/** Derive the pending/approved/rejected badge from the record's real status +
 *  resolution decision. `status` is pending | resolved | escalated; the
 *  approve-vs-reject split lives in `resolution.decision`. */
function approvalView(a: ApprovalRecord): { label: string; tone: string; decided: boolean } {
  const status = a.status ?? "pending";
  if (status === "pending") return { label: "pending", tone: "warning", decided: false };
  if (status === "resolved") {
    const decided = a.resolution?.decision;
    if (decided === "reject") return { label: "rejected", tone: "danger", decided: true };
    return { label: "approved", tone: "success", decided: true }; // approve | edit_and_approve
  }
  return { label: "escalated", tone: "warning", decided: !!a.resolution };
}

export default function ApprovalsPage() {
  const load = useLoad<ApprovalRecord[]>(listApprovals);
  const [connected, setConnected] = useState(false);
  useEffect(() => setConnected(isConfigured()), []);

  const approvals = load.data ?? [];

  return (
    <div style={{ maxWidth: 980, margin: "0 auto", padding: "28px 28px 48px" }}>
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Approvals</h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          Resolve the human-approval gates holding your runs.
        </p>
      </header>

      {!connected ? (
        <ConnectNote />
      ) : load.loading && !load.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Skeleton height={132} radius={8} />
          <Skeleton height={132} radius={8} />
        </div>
      ) : load.error ? (
        <ErrorNote message={load.error} onRetry={load.reload} />
      ) : approvals.length === 0 ? (
        <Card pad={20}>
          <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
            No approvals pending. Runs pause here when they reach an{" "}
            <span style={{ color: "var(--nt-approval)" }}>approval</span> node.
          </div>
        </Card>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {approvals.map((a) => (
            <ApprovalCard key={a.approval_id ?? `${a.node_id}:${a.run_id}`} approval={a} onResolved={load.reload} />
          ))}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({
  approval,
  onResolved,
}: {
  approval: ApprovalRecord;
  onResolved: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  // Optimistic flip: show the decided state the instant the reviewer clicks,
  // before the refetch replaces it with the canonical resolved record.
  const [optimistic, setOptimistic] = useState<Decision | null>(null);

  const view = optimistic
    ? {
        label: optimistic === "reject" ? "rejected" : "approved",
        tone: optimistic === "reject" ? "danger" : "success",
        decided: true,
      }
    : approvalView(approval);

  async function resolve(decision: Decision) {
    if (!approval.approval_id) return;
    setBusy(decision);
    setError(null);
    setOptimistic(decision);
    try {
      const trimmedReason = reason.trim();
      await resolveApproval(approval.approval_id, {
        decision,
        ...(trimmedReason ? { reason: trimmedReason } : {}),
      });
      toast(
        decision === "approve"
          ? "Approved — the run resumes past the gate"
          : "Rejected — the run fails at the gate",
      );
      onResolved();
    } catch (e) {
      setOptimistic(null); // revert the optimistic flip
      setError(errMsg(e));
    } finally {
      setBusy(null);
    }
  }

  const payload = approval.proposed_payload ?? approval.context_excerpt ?? null;
  const payloadJson = payload ? JSON.stringify(payload, null, 2) : "— no payload —";
  const runShort = approval.run_id ? approval.run_id.slice(0, 8) : "—";

  return (
    <Card
      data-evidence-id={`approvals.card.${approval.approval_id}`}
      pad={16}
      style={{
        border: view.decided ? "1px solid var(--hair)" : "1px solid rgba(252,211,77,0.35)",
      }}
    >
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          aria-hidden
          style={{ width: 12, height: 12, borderRadius: 2, background: "var(--nt-approval)", flexShrink: 0 }}
        />
        <span style={{ fontFamily: MONO, fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
          {approval.node_id}
        </span>
        <span
          style={{
            fontFamily: MONO,
            fontSize: 11.5,
            color: "var(--text-faint)",
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          in {approval.graph_version_ref} ·{" "}
          {approval.run_id ? (
            <Link
              href={`/runs?run=${encodeURIComponent(approval.run_id)}`}
              title={approval.run_id}
              style={{ color: "var(--accent)", textDecoration: "none" }}
            >
              {runShort}
            </Link>
          ) : (
            runShort
          )}
        </span>
        <span style={{ marginLeft: "auto", flexShrink: 0 }}>
          <Pill tone={view.tone}>{view.label}</Pill>
        </span>
      </div>

      {approval.summary && (
        <p style={{ marginTop: 8, fontSize: 12.5, color: "var(--text-secondary)" }}>{approval.summary}</p>
      )}

      {/* Body: payload | action rail */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 220px", gap: 16, marginTop: 12 }}>
        <div style={{ minWidth: 0 }}>
          <CodeBlock label="Payload under review" code={payloadJson} />
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {view.decided ? (
            <DecisionRecord approval={approval} optimistic={optimistic} label={view.label} />
          ) : (
            <>
              <label
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 5,
                  fontSize: 11.5,
                  color: "var(--text-muted)",
                }}
              >
                Decision reason
                <textarea
                  aria-label="Decision reason"
                  data-evidence-id={`approvals.reason.${approval.approval_id}`}
                  value={reason}
                  maxLength={1000}
                  rows={3}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="Record why this branch is approved or rejected"
                  style={{
                    width: "100%",
                    resize: "vertical",
                    minHeight: 62,
                    border: "1px solid var(--hair-strong)",
                    borderRadius: 8,
                    background: "var(--bg-card)",
                    color: "var(--text-primary)",
                    font: "inherit",
                    lineHeight: 1.45,
                    padding: "8px 9px",
                  }}
                />
              </label>
              <Button
                variant="primary"
                onClick={() => resolve("approve")}
                disabled={busy !== null}
                data-evidence-id={`approvals.approve.${approval.approval_id}`}
                style={{ width: "100%" }}
              >
                {busy === "approve" ? "Approving…" : "Approve"}
              </Button>
              <Button
                variant="danger"
                onClick={() => resolve("reject")}
                disabled={busy !== null}
                data-evidence-id={`approvals.reject.${approval.approval_id}`}
                style={{ width: "100%" }}
              >
                {busy === "reject" ? "Rejecting…" : "Reject"}
              </Button>
              <p style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5, marginTop: 2 }}>
                Requires reviewer role. Recorded to the audit chain.
              </p>
              {error && (
                <p style={{ fontSize: 11.5, color: "var(--danger)", lineHeight: 1.5 }}>{error}</p>
              )}
            </>
          )}
        </div>
      </div>
    </Card>
  );
}

function DecisionRecord({
  approval,
  optimistic,
  label,
}: {
  approval: ApprovalRecord;
  optimistic: "approve" | "reject" | null;
  label: string;
}) {
  const res = approval.resolution;
  const who = optimistic ? "you" : (res?.actor?.subject ?? "—");
  const when = optimistic ? "just now" : fmtClock(res?.resolved_at);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <MonoLabel>Decision</MonoLabel>
      <div style={{ fontSize: 13, color: "var(--text-primary)", textTransform: "capitalize" }}>{label}</div>
      <div style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-muted)" }}>
        by {who}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-faint)" }}>{when}</div>
      {res?.edited_payload != null && (
        <div style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>
          Payload edited before approval.
        </div>
      )}
      {res?.reason && (
        <div style={{ fontSize: 11.5, color: "var(--text-muted)", lineHeight: 1.5 }}>
          {res.reason}
        </div>
      )}
    </div>
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
