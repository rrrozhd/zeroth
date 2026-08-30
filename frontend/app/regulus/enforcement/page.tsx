"use client";

// Regulus Enforcement — the admin-gated control-plane surface for enforcement
// actions and the automated policy-action history.
//
// Two independent reads via `useLoad`, each degrading to inline error / empty
// states rather than throwing a boundary. Enforcement actions in the `pending`
// state expose Approve / Reject — both MUTATE the Regulus control plane through
// the console proxy, so each is gated behind a window.confirm, carries an
// optional decision reason, toasts on success, and refetches so the list and
// any sidebar counters catch up. Authentication uses the short-lived HttpOnly
// session cookie; the exchanged API key is never persisted, logged, or placed
// in a URL.

import { useEffect, useState } from "react";
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
import { errMsg, getIdentity } from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";
import { regulusAccess, type RegulusAccess } from "@/app/regulus/regulus-access";
import styles from "../subpages.module.css";

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
  const [connected, setConnected] = useState(false);
  useEffect(() => setConnected(isConfigured()), []);

  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Enforcement"
        description="Review pending decisions and the automated policy-action history."
      />
      {connected ? <EnforcementAccessBoundary /> : <EnforcementDisconnected />}
    </ConsolePage>
  );
}

function EnforcementDisconnected() {
  return (
    <>
      <ConsoleSection title="Enforcement actions"><ConnectNote /></ConsoleSection>
      <ConsoleSection title="Policy actions"><ConnectNote /></ConsoleSection>
    </>
  );
}

function EnforcementAccessBoundary() {
  const identity = useLoad(getIdentity);
  if (identity.loading && !identity.data) return <Skeleton height={74} radius={8} />;

  const access = regulusAccess(identity.data, identity.error);
  if (!access.canRead) {
    return (
      <>
        <ScopeNotice access={access} />
        <div data-evidence-id="regulus.enforcement.access.restricted">
          <ConsoleNotice title="Enforcement access restricted">
            This role does not include metrics:read. Enforcement and capability records are
            hidden for this credential and no protected read was issued.
          </ConsoleNotice>
        </div>
      </>
    );
  }
  return <EnforcementData access={access} />;
}

function ScopeNotice({ access }: { access: RegulusAccess }) {
  return (
    <div
      data-evidence-id="regulus.enforcement.scope"
      data-decision-access={access.canMutate ? "enabled" : "read-only"}
    >
      <ConsoleNotice title="Scope and authorization">
        Scope: {access.scope ?? "unavailable"} · Role: {access.roles} · Decision access:{" "}
        {access.canMutate ? "enabled" : "read-only"}
      </ConsoleNotice>
    </div>
  );
}

function EnforcementData({ access }: { access: RegulusAccess }) {
  const actions = useLoad<EnforcementActionOut[]>(rgEnforcementActions);
  const policies = useLoad<PolicyActionOut[]>(rgPolicyActions);
  const actionRows = actions.data ?? [];
  const policyRows = policies.data ?? [];

  return (
    <>
      <ScopeNotice access={access} />

      {/* ── Enforcement actions ─────────────────────────────────────────── */}
      <ConsoleSection
        title="Enforcement actions"
        meta={actionRows.length}
      >
        {actions.loading && !actions.data ? (
          <div className={styles.loadingStack}>
            <Skeleton height={148} radius={8} />
            <Skeleton height={148} radius={8} />
          </div>
        ) : actions.error ? (
          <ErrorNote message={actions.error} onRetry={actions.reload} />
        ) : actionRows.length === 0 ? (
          <EmptyNote>No enforcement actions.</EmptyNote>
        ) : (
          <div className={styles.panelStack}>
            {actionRows.map((a) => (
              <EnforcementActionCard
                key={a.id}
                action={a}
                canMutate={access.canMutate}
                onDecided={actions.reload}
              />
            ))}
          </div>
        )}
      </ConsoleSection>

      {/* ── Policy actions ──────────────────────────────────────────────── */}
      <ConsoleSection
        title="Policy actions"
        meta={policyRows.length}
      >
        <p className={styles.sectionNote}>
          Automated policy actions proposed by Regulus and their lifecycle.
        </p>

        {policies.loading && !policies.data ? (
          <Skeleton height={180} radius={8} />
        ) : policies.error ? (
          <ErrorNote message={policies.error} onRetry={policies.reload} />
        ) : policyRows.length === 0 ? (
          <EmptyNote>No policy actions.</EmptyNote>
        ) : (
          <PolicyActionsTable rows={policyRows} />
        )}
      </ConsoleSection>
    </>
  );
}

// ── Enforcement action card ──────────────────────────────────────────────

type Decision = "approve" | "reject";

function EnforcementActionCard({
  action,
  canMutate,
  onDecided,
}: {
  action: EnforcementActionOut;
  canMutate: boolean;
  onDecided: () => void;
}) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);

  const status = action.status ?? "pending";
  const tone = ENF_TONE[status] ?? "neutral";
  const decided = status !== "pending";
  const syntheticDemo =
    (action.reason?.startsWith("[SYNTHETIC DEMO]") ?? false) ||
    action.before_config?.demo_fixture === true;

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
    <div data-evidence-id={`regulus.enforcement.action.${action.id}`}>
      <ConsoleSurface
        className={styles.actionRecord}
        evidenceScope={`enforcement-${action.id}`}
      >
      {/* Header: dot · id · type · status pill */}
      <div className={styles.recordHeader}>
        <StatusDot tone={tone} pulse={!decided} />
        <span className={styles.recordIdentity}>
          #{action.id}
        </span>
        <span className={styles.recordType}>
          {action.action_type}
        </span>
        {syntheticDemo ? <Pill tone="info">synthetic demo</Pill> : null}
        <span
          className={styles.recordCapability}
          title={action.capability_id}
        >
          · {action.capability_id}
        </span>
        <span className={styles.recordStatus}>
          <Pill tone={tone}>{status}</Pill>
        </span>
      </div>

      {/* Proposal reason */}
      {action.reason && (
        <p className={styles.recordReason}>
          {action.reason}
        </p>
      )}
      <div className={styles.recordMeta}>
        proposed {fmtDateTime(action.created_at)}
      </div>

      {/* Body: config detail | decision rail */}
      <div className={styles.actionBody}>
        <div className={styles.actionMain}>
          {hasConfig ? (
            <CodeBlock
              label="Proposed change (before → after)"
              ariaLabel={`Proposed change for enforcement action ${action.id}`}
              code={configJson}
            />
          ) : (
            <ConsoleEmpty>No configuration payload.</ConsoleEmpty>
          )}
        </div>

        <div className={styles.decisionRail}>
          {decided ? (
            <DecisionRecord action={action} label={status} tone={tone} />
          ) : !canMutate ? (
            <div
              data-evidence-id="regulus.enforcement.mutation.restricted"
              className={styles.decisionHelp}
            >
              Read-only. This role does not include econ:admin; only platform administrators or
              configured roles granted that permission may approve or reject.
            </div>
          ) : (
            <>
              <ConsoleInput
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Reason (optional)"
                disabled={busy !== null}
                aria-label="Decision reason"
                data-evidence-id={`regulus.enforcement.action.${action.id}.reason`}
              />
              <Button
                type="button"
                variant="primary"
                onClick={() => decide("approve")}
                disabled={busy !== null}
                data-evidence-id={`regulus.enforcement.action.${action.id}.approve`}
              >
                {busy === "approve" ? "Approving…" : "Approve"}
              </Button>
              <Button
                type="button"
                variant="danger"
                onClick={() => decide("reject")}
                disabled={busy !== null}
                data-evidence-id={`regulus.enforcement.action.${action.id}.reject`}
              >
                {busy === "reject" ? "Rejecting…" : "Reject"}
              </Button>
              <p className={styles.decisionHelp}>
                {syntheticDemo
                  ? "Demo-only investigation flag; no payment, email, traffic, or third-party action."
                  : "Admin-gated. Applies to the Regulus control plane."}
              </p>
              {error && (
                <p className={styles.inlineError}>{error}</p>
              )}
            </>
          )}
        </div>
      </div>
      </ConsoleSurface>
    </div>
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
    <div className={styles.decisionRecord}>
      <MonoLabel>Decision</MonoLabel>
      <div>
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

function PolicyActionsTable({ rows }: { rows: PolicyActionOut[] }) {
  return (
    <ConsoleTableFrame ariaLabel="Enforcement actions">
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Id</th>
            <th>Capability</th>
            <th>Action type</th>
            <th>Status</th>
            <th>Proposed by</th>
            <th>Proposed at</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <PolicyRow key={p.id} policy={p} />
          ))}
        </tbody>
      </table>
    </ConsoleTableFrame>
  );
}

function PolicyRow({ policy: p }: { policy: PolicyActionOut }) {
  const tone = POLICY_TONE[p.status] ?? "neutral";
  return (
    <tr>
      <Cell mono strong>#{p.id}</Cell>
      <Cell mono title={p.capability_id}>{p.capability_id}</Cell>
      <Cell mono>{p.action_type}</Cell>
      <td><Pill tone={tone}>{p.status}</Pill></td>
      <Cell mono title={p.proposed_by}>{p.proposed_by || "—"}</Cell>
      <Cell mono faint>{fmtDateTime(p.proposed_at)}</Cell>
    </tr>
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
    <td
      title={title}
      className={mono ? styles.tableCellMono : undefined}
      style={{
        fontWeight: strong ? 600 : 400,
        color: faint ? "var(--text-faint)" : strong ? "var(--text-primary)" : "var(--text-secondary)",
      }}
    >
      {children}
    </td>
  );
}

// ── Shared states ────────────────────────────────────────────────────────

function ConnectNote() {
  return (
    <ConsoleNotice title="Not connected">
      Open Connect in the sidebar to set the API base and key.
    </ConsoleNotice>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return <ConsoleEmpty>{children}</ConsoleEmpty>;
}

function ErrorNote({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <ConsoleNotice
      tone="danger"
      title="Enforcement data unavailable"
      actions={<Button variant="neutral" onClick={onRetry}>Retry</Button>}
    >
      {message}
    </ConsoleNotice>
  );
}
