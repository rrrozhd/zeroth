"use client";

// Regulus Enforcement — the admin-gated control-plane surface for enforcement
// actions and the automated policy-action history.
//
// Two independent reads via `useLoad`, each degrading to inline error / empty
// states rather than throwing a boundary. Enforcement actions in the `pending`
// state expose Approve / Reject — both MUTATE the Regulus control plane through
// the console proxy, so each is gated behind a window.confirm, carries an
// optional decision reason, toasts on success, and refetches so the list and
// any sidebar counters catch up. The API key lives only in localStorage and is
// attached as an X-API-Key header inside lib/api — never logged, never in a URL.

import { useEffect, useState } from "react";
import {
  Button,
  Card,
  CodeBlock,
  MonoLabel,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad } from "@/app/hooks/useLoad";
import {
  rgApproveAction,
  rgEnforcementActions,
  rgPolicyActions,
  rgRejectAction,
  type EnforcementActionOut,
  type PolicyActionOut,
} from "@/app/lib/regulusApi";
import { errMsg } from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

const MONO = "var(--font-mono)";

/** Locale date+time (24h), or "—" for a missing/unparseable timestamp. */
function fmtDateTime(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString([], { hour12: false });
}

/** enforcement status (pending|approved|rejected) → Pill tone. */
const ENF_TONE: Record<string, string> = {
  pending: "warning",
  approved: "success",
  rejected: "danger",
};

/** policy status (PROPOSED|APPROVED|APPLIED|REJECTED|FAILED) → Pill tone. */
const POLICY_TONE: Record<string, string> = {
  PROPOSED: "warning",
  APPROVED: "info",
  APPLIED: "success",
  REJECTED: "danger",
  FAILED: "danger",
};

export default function EnforcementPage() {
  const actions = useLoad<EnforcementActionOut[]>(rgEnforcementActions);
  const policies = useLoad<PolicyActionOut[]>(rgPolicyActions);
  const [connected, setConnected] = useState(false);
  useEffect(() => setConnected(isConfigured()), []);

  const actionRows = actions.data ?? [];
  const policyRows = policies.data ?? [];

  return (
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Enforcement</h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          Review enforcement actions awaiting a decision, and the automated policy-action history.
        </p>
      </header>

      {/* ── Enforcement actions ─────────────────────────────────────────── */}
      <SectionHeading count={connected ? actionRows.length : undefined}>
        Enforcement actions
      </SectionHeading>

      {!connected ? (
        <ConnectNote />
      ) : actions.loading && !actions.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Skeleton height={148} radius={8} />
          <Skeleton height={148} radius={8} />
        </div>
      ) : actions.error ? (
        <ErrorNote message={actions.error} onRetry={actions.reload} />
      ) : actionRows.length === 0 ? (
        <EmptyNote>No enforcement actions.</EmptyNote>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {actionRows.map((a) => (
            <EnforcementActionCard key={a.id} action={a} onDecided={actions.reload} />
          ))}
        </div>
      )}

      {/* ── Policy actions ──────────────────────────────────────────────── */}
      <SectionHeading count={connected ? policyRows.length : undefined} style={{ marginTop: 34 }}>
        Policy actions
      </SectionHeading>
      <p style={{ margin: "0 0 12px", fontSize: 12.5, color: "var(--text-muted)" }}>
        Automated policy actions proposed by Regulus and their lifecycle.
      </p>

      {!connected ? (
        <ConnectNote />
      ) : policies.loading && !policies.data ? (
        <Skeleton height={180} radius={8} />
      ) : policies.error ? (
        <ErrorNote message={policies.error} onRetry={policies.reload} />
      ) : policyRows.length === 0 ? (
        <EmptyNote>No policy actions.</EmptyNote>
      ) : (
        <PolicyActionsTable rows={policyRows} />
      )}
    </div>
  );
}

// ── Enforcement action card ──────────────────────────────────────────────

type Decision = "approve" | "reject";

function EnforcementActionCard({
  action,
  onDecided,
}: {
  action: EnforcementActionOut;
  onDecided: () => void;
}) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);

  const status = action.status ?? "pending";
  const tone = ENF_TONE[status] ?? "neutral";
  const decided = status !== "pending";

  const before = action.before_config ?? {};
  const after = action.after_config ?? {};
  const hasConfig = Object.keys(before).length > 0 || Object.keys(after).length > 0;
  const configJson = JSON.stringify({ before, after }, null, 2);

  async function decide(kind: Decision) {
    const verb = kind === "approve" ? "Approve" : "Reject";
    const ok = window.confirm(
      `${verb} enforcement action #${action.id} (${action.action_type}) on ` +
        `capability ${action.capability_id}?\n\nThis changes the Regulus control plane.`,
    );
    if (!ok) return;

    setBusy(kind);
    setError(null);
    const note = reason.trim();
    try {
      if (kind === "approve") await rgApproveAction(action.id, note || undefined);
      else await rgRejectAction(action.id, note || undefined);
      toast(
        kind === "approve"
          ? `Action #${action.id} approved — enforcement will apply`
          : `Action #${action.id} rejected`,
      );
      onDecided();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      pad={16}
      style={{
        border: decided ? "1px solid var(--hair)" : "1px solid rgba(252,211,77,0.35)",
      }}
    >
      {/* Header: dot · id · type · status pill */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <StatusDot tone={tone} pulse={!decided} />
        <span style={{ fontFamily: MONO, fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
          #{action.id}
        </span>
        <span style={{ fontFamily: MONO, fontSize: 12.5, color: "var(--text-secondary)" }}>
          {action.action_type}
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
          title={action.capability_id}
        >
          · {action.capability_id}
        </span>
        <span style={{ marginLeft: "auto", flexShrink: 0 }}>
          <Pill tone={tone}>{status}</Pill>
        </span>
      </div>

      {/* Proposal reason */}
      {action.reason && (
        <p style={{ marginTop: 8, fontSize: 12.5, color: "var(--text-secondary)", lineHeight: 1.5 }}>
          {action.reason}
        </p>
      )}
      <div style={{ marginTop: 6, fontFamily: MONO, fontSize: 11, color: "var(--text-faint)" }}>
        proposed {fmtDateTime(action.created_at)}
      </div>

      {/* Body: config detail | decision rail */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 248px", gap: 16, marginTop: 12 }}>
        <div style={{ minWidth: 0 }}>
          {hasConfig ? (
            <CodeBlock label="Proposed change (before → after)" code={configJson} />
          ) : (
            <div
              style={{
                fontSize: 12,
                color: "var(--text-faint)",
                border: "1px dashed var(--hair)",
                borderRadius: 8,
                padding: "12px 14px",
              }}
            >
              No configuration payload.
            </div>
          )}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {decided ? (
            <DecisionRecord action={action} label={status} tone={tone} />
          ) : (
            <>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Reason (optional)"
                disabled={busy !== null}
                aria-label="Decision reason"
                style={{
                  fontFamily: MONO,
                  fontSize: 12,
                  color: "var(--text-primary)",
                  background: "var(--bg-raised)",
                  border: "1px solid var(--hair-strong)",
                  borderRadius: 6,
                  padding: "7px 9px",
                  width: "100%",
                  outline: "none",
                }}
              />
              <Button
                type="button"
                variant="primary"
                onClick={() => decide("approve")}
                disabled={busy !== null}
                style={{ width: "100%" }}
              >
                {busy === "approve" ? "Approving…" : "Approve"}
              </Button>
              <Button
                type="button"
                variant="danger"
                onClick={() => decide("reject")}
                disabled={busy !== null}
                style={{ width: "100%" }}
              >
                {busy === "reject" ? "Rejecting…" : "Reject"}
              </Button>
              <p style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5, marginTop: 2 }}>
                Admin-gated. Applies to the Regulus control plane.
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

/** who / when / reason for an already-decided action, in place of the buttons. */
function DecisionRecord({
  action,
  label,
  tone,
}: {
  action: EnforcementActionOut;
  label: string;
  tone: string;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <MonoLabel>Decision</MonoLabel>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Pill tone={tone}>{label}</Pill>
      </div>
      <div style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-muted)" }}>
        by {action.approver_sub ?? "—"}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-faint)" }}>
        {fmtDateTime(action.approved_at)}
      </div>
      {action.reason && (
        <div style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>
          “{action.reason}”
        </div>
      )}
    </div>
  );
}

// ── Policy actions table ─────────────────────────────────────────────────

const POLICY_COLS = "56px 1.4fr 1.1fr 96px 1fr 1.1fr";

function PolicyActionsTable({ rows }: { rows: PolicyActionOut[] }) {
  return (
    <Card pad={0} style={{ overflow: "hidden" }}>
      <div style={{ overflowX: "auto" }}>
        <div style={{ minWidth: 760 }}>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: POLICY_COLS,
              gap: 12,
              padding: "10px 16px",
              borderBottom: "1px solid var(--hair)",
            }}
          >
            <ColHead>id</ColHead>
            <ColHead>capability</ColHead>
            <ColHead>action type</ColHead>
            <ColHead>status</ColHead>
            <ColHead>proposed by</ColHead>
            <ColHead>proposed at</ColHead>
          </div>
          {rows.map((p) => (
            <PolicyRow key={p.id} policy={p} />
          ))}
        </div>
      </div>
    </Card>
  );
}

function PolicyRow({ policy: p }: { policy: PolicyActionOut }) {
  const tone = POLICY_TONE[p.status] ?? "neutral";
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: POLICY_COLS,
        gap: 12,
        alignItems: "center",
        padding: "11px 16px",
        borderBottom: "1px solid var(--hair)",
      }}
    >
      <Cell mono strong>
        #{p.id}
      </Cell>
      <Cell mono title={p.capability_id}>
        {p.capability_id}
      </Cell>
      <Cell mono>{p.action_type}</Cell>
      <div style={{ minWidth: 0 }}>
        <Pill tone={tone}>{p.status}</Pill>
      </div>
      <Cell mono title={p.proposed_by}>
        {p.proposed_by || "—"}
      </Cell>
      <Cell mono faint>
        {fmtDateTime(p.proposed_at)}
      </Cell>
    </div>
  );
}

function Cell({
  children,
  mono = false,
  strong = false,
  faint = false,
  title,
}: {
  children: React.ReactNode;
  mono?: boolean;
  strong?: boolean;
  faint?: boolean;
  title?: string;
}) {
  return (
    <span
      title={title}
      style={{
        minWidth: 0,
        fontFamily: mono ? MONO : "inherit",
        fontSize: 12.5,
        fontWeight: strong ? 600 : 400,
        color: faint ? "var(--text-faint)" : strong ? "var(--text-primary)" : "var(--text-secondary)",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      {children}
    </span>
  );
}

function ColHead({ children }: { children: React.ReactNode }) {
  return <MonoLabel>{children}</MonoLabel>;
}

// ── Shared states ────────────────────────────────────────────────────────

function SectionHeading({
  children,
  count,
  style,
}: {
  children: React.ReactNode;
  count?: number;
  style?: React.CSSProperties;
}) {
  return (
    <h2
      style={{
        display: "flex",
        alignItems: "baseline",
        gap: 8,
        fontSize: 14,
        fontWeight: 600,
        margin: "0 0 12px",
        ...style,
      }}
    >
      {children}
      {count != null && (
        <span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 500, color: "var(--text-faint)" }}>
          {count}
        </span>
      )}
    </h2>
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

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <Card pad={20}>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>{children}</div>
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
