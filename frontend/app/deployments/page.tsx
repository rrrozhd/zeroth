"use client";

// The Deployments screen — a master-detail operator view over the deployment
// registry. This is the "all functions" surface for a deployed graph version:
// metadata, public contracts, the signed attestation (+ verification), the
// review evidence bundle, the version/audit timeline, cumulative cost, and a
// jump to the full audit chain.
//
// Left: every persisted deployment version (listDeployments). Right: the
// selected deployment's detail, or the inline "create deployment" form.
//
// Each detail panel owns its loading/empty/error state via its own `useLoad`,
// so one panel 404-ing (e.g. no attestation yet) never blanks the others and
// never crashes the screen. Every mutation (rollback, create, verify) toasts.
// The API key lives only in localStorage (lib/config) — never logged, never in
// a URL.
//
// ROLLBACK TARGET: the rollback endpoint pins a new version to an earlier GRAPH
// version (an int). There is no clean "prior graph versions" list on the wire —
// getDeploymentTimeline returns per-node audit records, not a version history,
// and listDeployments only carries each row's own `graph_version_ref`
// (`{graph_id}@{version}`). So the target is chosen with a numeric input,
// bounded to [1, current-1] and defaulted to current-1, where `current` is
// parsed from the selected row's graph_version_ref (matching the P0 Overview's
// `split("@")` derivation, but with a real range + validation).

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
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
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  createDeployment,
  errMsg,
  getAttestationOf,
  getAttestationVerifyOf,
  getCostOf,
  getDeploymentEvidence,
  getDeploymentMetadata,
  getDeploymentTimeline,
  getInputContract,
  getOutputContract,
  getResultErrorStateSchema,
  listDeployments,
  listCertifications,
  postVerifyAttestationOf,
  rollbackDeployment,
  type AttestationVerification,
  type AuditTimeline,
  type DeploymentAttestation,
  type DeploymentCost,
  type DeploymentEvidence,
  type DeploymentMetadata,
  type DeploymentResultErrorStateSchema,
  type DeploymentSummary,
  type CertificationResponse,
  type NodeAuditRecord,
  type PublicContractSchema,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

// --------------------------------------------------------------------------
// Shared helpers (mirrors the Runs screen conventions)
// --------------------------------------------------------------------------

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

function keyOf(d: DeploymentSummary): string {
  return `${d.deployment_ref}::${d.version}`;
}

/** The graph version encoded in a `{graph_id}@{version}` ref, if parseable. */
function graphVersionOf(graphVersionRef: string): number | null {
  const n = Number(graphVersionRef.split("@").pop());
  return Number.isInteger(n) && n > 0 ? n : null;
}

function nodeTypeOf(rec: NodeAuditRecord): string | null {
  const meta = rec.execution_metadata as Record<string, unknown> | undefined;
  const t = meta?.node_type;
  return typeof t === "string" ? t : null;
}

function fmtCost(n: number): string {
  return `$${n.toFixed(4)}`;
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

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function DeploymentsPage() {
  const deployments = useLoad<DeploymentSummary[]>(listDeployments);

  // localStorage-derived config is read after mount so the static prerender and
  // the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = deployments.data ?? [];
  const selected = useMemo(
    () => list.find((d) => keyOf(d) === selectedKey) ?? null,
    [list, selectedKey],
  );

  function select(d: DeploymentSummary) {
    setCreating(false);
    setSelectedKey(keyOf(d));
  }

  function openCreate() {
    setCreating(true);
    setSelectedKey(null);
  }

  // After a create, refresh the list and select the freshly-registered version.
  function onCreated(d: DeploymentSummary) {
    setCreating(false);
    setSelectedKey(keyOf(d));
    deployments.reload();
  }

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0 }}>
      <ListPane
        deployments={deployments}
        connected={connected}
        mounted={mounted}
        selectedKey={selectedKey}
        onSelect={select}
        onNew={openCreate}
      />
      <div style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
        {creating ? (
          <CreateForm onCreated={onCreated} onCancel={() => setCreating(false)} />
        ) : selected ? (
          <DeploymentDetail
            key={selectedKey ?? ""}
            deployment={selected}
            onRolledBack={deployments.reload}
          />
        ) : (
          <DetailPlaceholder />
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Left list (~300px)
// --------------------------------------------------------------------------

function ListPane({
  deployments,
  connected,
  mounted,
  selectedKey,
  onSelect,
  onNew,
}: {
  deployments: Loadable<DeploymentSummary[]>;
  connected: boolean;
  mounted: boolean;
  selectedKey: string | null;
  onSelect: (d: DeploymentSummary) => void;
  onNew: () => void;
}) {
  const list = deployments.data ?? [];
  return (
    <aside
      style={{
        width: 300,
        flexShrink: 0,
        borderRight: "1px solid var(--hair)",
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        background: "var(--bg-chrome)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          padding: "13px 14px",
          borderBottom: "1px solid var(--hair)",
        }}
      >
        <MonoLabel>Deployments</MonoLabel>
        <Button variant="primary" onClick={onNew} style={{ padding: "4px 9px" }}>
          + New
        </Button>
      </div>

      <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
        {deployments.loading && !deployments.data ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, padding: 14 }}>
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} height={40} />
            ))}
          </div>
        ) : deployments.error ? (
          <div style={{ padding: 14 }}>
            <InlineError message={deployments.error} onRetry={deployments.reload} />
          </div>
        ) : mounted && !connected ? (
          <EmptyNote>Connect to the API (top bar) to load deployments.</EmptyNote>
        ) : list.length === 0 ? (
          <EmptyNote>
            No deployments yet — register one with <b>+ New</b>, or author a graph in{" "}
            <Link href="/studio" style={{ color: "var(--accent)", textDecoration: "none" }}>
              Studio
            </Link>
            .
          </EmptyNote>
        ) : (
          list.map((d) => (
            <DeploymentRow
              key={keyOf(d)}
              deployment={d}
              selected={keyOf(d) === selectedKey}
              onSelect={() => onSelect(d)}
            />
          ))
        )}
      </div>
    </aside>
  );
}

function DeploymentRow({
  deployment: d,
  selected,
  onSelect,
}: {
  deployment: DeploymentSummary;
  selected: boolean;
  onSelect: () => void;
}) {
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
          {d.deployment_ref}
        </span>
        <Pill tone={d.serving ? "success" : "neutral"} style={{ flexShrink: 0 }}>
          {d.serving ? "serving" : "registered"}
        </Pill>
      </div>
      <div
        style={{
          marginTop: 4,
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--text-faint)",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {d.graph_version_ref}
        <span style={{ color: "var(--text-muted)" }}> · v{d.version}</span>
      </div>
    </button>
  );
}

// --------------------------------------------------------------------------
// Create deployment — CreateDeploymentRequest is a simple 3-field shape
// ({ deployment_ref, graph_id, graph_version? }), not an embedded workflow
// definition, so a minimal inline form is the right call (rather than punting to
// Studio). `graph_id` references a graph *published* in Studio — the form links
// there rather than reimplementing a graph picker.
// --------------------------------------------------------------------------

function CreateForm({
  onCreated,
  onCancel,
}: {
  onCreated: (d: DeploymentSummary) => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const [deploymentRef, setDeploymentRef] = useState("");
  const [graphId, setGraphId] = useState("");
  const [graphVersion, setGraphVersion] = useState("");
  const [busy, setBusy] = useState(false);

  const canSubmit = deploymentRef.trim().length > 0 && graphId.trim().length > 0 && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    try {
      const gv = graphVersion.trim();
      const parsed = gv === "" ? null : Number(gv);
      if (parsed != null && !Number.isInteger(parsed)) {
        toast("Graph version must be a whole number.");
        setBusy(false);
        return;
      }
      const created = await createDeployment({
        deployment_ref: deploymentRef.trim(),
        graph_id: graphId.trim(),
        graph_version: parsed,
      });
      toast(`Registered ${created.deployment_ref} · ${created.graph_version_ref}`);
      onCreated(created);
    } catch (err) {
      toast(`Create failed: ${errMsg(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ padding: "22px 26px", maxWidth: 620 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
        <span style={{ fontSize: 17, fontWeight: 600 }}>New deployment</span>
      </div>
      <p style={{ margin: "0 0 16px", fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>
        Register a new deployment version pinned to a published graph.{" "}
        <code style={{ fontFamily: "var(--font-mono)" }}>graph_id</code> is a graph you published in{" "}
        <Link href="/studio" style={{ color: "var(--accent)", textDecoration: "none" }}>
          Studio
        </Link>
        . Registering does not start serving it — that still requires a restart with{" "}
        <code style={{ fontFamily: "var(--font-mono)" }}>ZEROTH_DEPLOYMENT_REF</code> set.
      </p>

      <Card pad={16}>
        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Field
            label="deployment_ref"
            hint="Logical name for this deployment (e.g. support-triage-prod)."
          >
            <TextInput
              value={deploymentRef}
              onChange={setDeploymentRef}
              placeholder="my-deployment"
              autoFocus
            />
          </Field>
          <Field label="graph_id" hint="Id of a published graph from Studio.">
            <TextInput value={graphId} onChange={setGraphId} placeholder="graph_abc123" />
          </Field>
          <Field
            label="graph_version"
            hint="Optional — omit to pin the latest published version."
          >
            <TextInput
              value={graphVersion}
              onChange={setGraphVersion}
              placeholder="latest"
              inputMode="numeric"
            />
          </Field>
          <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
            <Button type="submit" variant="primary" disabled={!canSubmit}>
              {busy ? "Registering…" : "Register deployment"}
            </Button>
            <Button type="button" variant="neutral" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "block" }}>
      <MonoLabel style={{ display: "block", marginBottom: 5 }}>{label}</MonoLabel>
      {children}
      {hint && (
        <span
          style={{
            display: "block",
            marginTop: 5,
            fontSize: 11,
            color: "var(--text-faint)",
            lineHeight: 1.5,
          }}
        >
          {hint}
        </span>
      )}
    </label>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  autoFocus,
  inputMode,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  inputMode?: "numeric";
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      inputMode={inputMode}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "8px 10px",
        outline: "none",
      }}
    />
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
      Select a deployment to inspect.
    </div>
  );
}

function DeploymentDetail({
  deployment: d,
  onRolledBack,
}: {
  deployment: DeploymentSummary;
  onRolledBack: () => void;
}) {
  return (
    <div style={{ padding: "22px 26px", display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Header: ref + version + serving + rollback */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 17, fontWeight: 600 }}>
          {d.deployment_ref}
        </span>
        <Pill tone="accent">v{d.version}</Pill>
        <Pill tone={d.serving ? "success" : "neutral"}>
          {d.serving ? "serving" : "registered"}
        </Pill>
        {d.status && d.status !== "active" && <Pill tone="muted">{d.status}</Pill>}
        <div style={{ marginLeft: "auto" }}>
          <RollbackControl deployment={d} onRolledBack={onRolledBack} />
        </div>
      </div>

      {/* Panels — each owns its own load/empty/error */}
      <CertificationPanel refId={d.deployment_ref} />
      <MetadataPanel refId={d.deployment_ref} />
      <ContractsPanel refId={d.deployment_ref} />
      <AttestationPanel refId={d.deployment_ref} />
      <EvidencePanel refId={d.deployment_ref} />
      <TimelinePanel refId={d.deployment_ref} />
      <CostPanel refId={d.deployment_ref} />
      <AuditsPanel />
    </div>
  );
}

function CertificationPanel({ refId }: { refId: string }) {
  const certifications = useLoad<CertificationResponse[]>(listCertifications);
  const records = certifications.data ?? [];
  const record = records.find((item) => item.promotion_target_key === refId) ?? null;

  return (
    <Card label="Production certification" pad={14}>
      {certifications.loading && !certifications.data ? (
        <Skeleton height={64} />
      ) : certifications.error ? (
        <InlineError message={certifications.error} onRetry={certifications.reload} />
      ) : !record ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Pill tone="danger" style={{ alignSelf: "flex-start" }}>
            production blocked
          </Pill>
          <span style={{ fontSize: 12.5, color: "var(--text-muted)" }}>
            No promoted certification owns this deployment target.
          </span>
          <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
            Remediation: configure the server-owned artifact identity, register a trusted
            production receipt, and promote it to this exact deployment reference.
          </span>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
            <Pill tone={record.evaluation.production_ready ? "success" : "danger"}>
              {record.evaluation.production_ready ? "production ready" : "production blocked"}
            </Pill>
            <Pill tone="neutral">{record.state}</Pill>
            {record.evaluation.test_deployable && <Pill tone="info">test deployable</Pill>}
            {record.evaluation.override_active && <Pill tone="warning">override active</Pill>}
          </div>
          {record.override && (
            <div style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
              Override [{record.override.scopes.join(", ")}] until{" "}
              {new Date(record.override.expires_at).toLocaleString()}: {record.override.reason}
            </div>
          )}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(100px, auto) 1fr",
              gap: "5px 12px",
              fontSize: 11.5,
            }}
          >
            <span style={{ color: "var(--text-faint)" }}>commit</span>
            <code style={{ color: "var(--text-muted)", overflowWrap: "anywhere" }}>
              {record.app_commit}
            </code>
            <span style={{ color: "var(--text-faint)" }}>image</span>
            <code style={{ color: "var(--text-muted)", overflowWrap: "anywhere" }}>
              {record.image_digest}
            </code>
          </div>
          {record.evaluation.blockers.map((blocker) => (
            <div
              key={blocker.code}
              style={{
                borderLeft: "2px solid var(--danger)",
                paddingLeft: 10,
                display: "flex",
                flexDirection: "column",
                gap: 3,
              }}
            >
              <code style={{ fontSize: 11.5, color: "var(--danger)" }}>{blocker.code}</code>
              <span style={{ fontSize: 12.5, color: "var(--text-primary)" }}>
                {blocker.message}
              </span>
              <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
                Remediation: {blocker.remediation}
              </span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Rollback
// --------------------------------------------------------------------------

function RollbackControl({
  deployment: d,
  onRolledBack,
}: {
  deployment: DeploymentSummary;
  onRolledBack: () => void;
}) {
  const toast = useToast();
  const current = graphVersionOf(d.graph_version_ref);
  const maxTarget = current != null ? current - 1 : null;
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<number>(maxTarget && maxTarget >= 1 ? maxTarget : 1);
  const [busy, setBusy] = useState(false);

  // No earlier graph version to fall back to (current is v1, or unparseable).
  if (current == null || maxTarget == null || maxTarget < 1) {
    return (
      <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
        {current == null
          ? "rollback: graph version unknown"
          : "at graph v1 — nothing earlier to roll back to"}
      </span>
    );
  }

  async function doRollback() {
    if (!Number.isInteger(target) || target < 1 || (maxTarget != null && target > maxTarget)) {
      toast(`Target must be a graph version between 1 and ${maxTarget}.`);
      return;
    }
    setBusy(true);
    try {
      await rollbackDeployment(d.deployment_ref, target);
      toast(`Registered rollback of ${d.deployment_ref} to graph v${target}`);
      setOpen(false);
      onRolledBack();
    } catch (e) {
      toast(`Rollback failed: ${errMsg(e)}`);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button variant="neutral" onClick={() => setOpen(true)}>
        Rollback
      </Button>
    );
  }

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>to graph v</span>
      <input
        type="number"
        min={1}
        max={maxTarget}
        value={target}
        onChange={(e) => setTarget(Math.trunc(Number(e.target.value)))}
        style={{
          width: 64,
          fontFamily: "var(--font-mono)",
          fontSize: 12.5,
          color: "var(--text-primary)",
          background: "var(--bg-code)",
          border: "1px solid var(--hair-strong)",
          borderRadius: 6,
          padding: "6px 8px",
          outline: "none",
        }}
      />
      <span style={{ fontSize: 11, color: "var(--text-faint)" }}>(current v{current})</span>
      <Button variant="danger" disabled={busy} onClick={doRollback}>
        {busy ? "…" : "Confirm"}
      </Button>
      <Button variant="neutral" disabled={busy} onClick={() => setOpen(false)}>
        Cancel
      </Button>
    </div>
  );
}

// --------------------------------------------------------------------------
// Metadata
// --------------------------------------------------------------------------

function MetadataPanel({ refId }: { refId: string }) {
  const meta = useLoad<DeploymentMetadata>(() => getDeploymentMetadata(refId));
  return (
    <Card label="Metadata" pad={14}>
      {meta.loading && !meta.data ? (
        <Skeleton height={90} />
      ) : meta.error ? (
        <InlineError message={meta.error} onRetry={meta.reload} />
      ) : meta.data ? (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "10px 28px", marginBottom: 12 }}>
            <Meta label="graph" value={meta.data.graph_version_ref} />
            <Meta label="graph id" value={meta.data.graph_id} />
            <Meta label="graph version" value={String(meta.data.graph_version)} />
            <Meta label="deployment version" value={String(meta.data.deployment_version)} />
            <Meta label="status" value={meta.data.status} />
            <Meta label="created" value={fmtDateTime(meta.data.created_at)} />
          </div>
          <CodeBlock label="Full metadata" code={jsonText(meta.data)} />
        </>
      ) : (
        <EmptyInline>No metadata.</EmptyInline>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Contracts — input + output + result/error-state schema, raw JSON
// --------------------------------------------------------------------------

function ContractsPanel({ refId }: { refId: string }) {
  const input = useLoad<PublicContractSchema>(() => getInputContract(refId));
  const output = useLoad<PublicContractSchema>(() => getOutputContract(refId));
  const resultError = useLoad<DeploymentResultErrorStateSchema>(() =>
    getResultErrorStateSchema(refId),
  );

  return (
    <Card label="Contracts" pad={14}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <ContractBlock label="Input contract" load={input} />
        <ContractBlock label="Output contract" load={output} />
        <ContractBlock label="Result + error-state schema" load={resultError} />
      </div>
    </Card>
  );
}

function ContractBlock<T>({ label, load }: { label: string; load: Loadable<T> }) {
  if (load.loading && !load.data) return <Skeleton height={70} />;
  if (load.error) return <InlineError message={load.error} onRetry={load.reload} />;
  return <CodeBlock label={label} code={jsonText(load.data)} />;
}

// --------------------------------------------------------------------------
// Attestation — the signed snapshot + two independent verify paths (GET
// self-verify, POST submitted-verify), each with an idle -> verifying -> result
// chip.
// --------------------------------------------------------------------------

type VerifyState =
  | { phase: "idle" }
  | { phase: "verifying" }
  | { phase: "done"; result: AttestationVerification }
  | { phase: "error"; msg: string };

function AttestationPanel({ refId }: { refId: string }) {
  const att = useLoad<DeploymentAttestation>(() => getAttestationOf(refId));
  const [getVerify, setGetVerify] = useState<VerifyState>({ phase: "idle" });
  const [postVerify, setPostVerify] = useState<VerifyState>({ phase: "idle" });

  async function run(
    fn: () => Promise<AttestationVerification>,
    set: (s: VerifyState) => void,
  ) {
    set({ phase: "verifying" });
    try {
      const result = await fn();
      set({ phase: "done", result });
    } catch (e) {
      set({ phase: "error", msg: errMsg(e) });
    }
  }

  return (
    <Card label="Attestation" pad={14}>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "10px 16px",
          marginBottom: 12,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Button
            variant="primary"
            disabled={getVerify.phase === "verifying"}
            onClick={() => run(() => getAttestationVerifyOf(refId), setGetVerify)}
          >
            {getVerify.phase === "verifying" ? "Verifying…" : "Verify (server)"}
          </Button>
          <AttVerifyChip state={getVerify} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Button
            variant="neutral"
            disabled={postVerify.phase === "verifying"}
            onClick={() => run(() => postVerifyAttestationOf(refId), setPostVerify)}
          >
            {postVerify.phase === "verifying" ? "Verifying…" : "Verify (submitted)"}
          </Button>
          <AttVerifyChip state={postVerify} />
        </div>
      </div>

      {att.loading && !att.data ? (
        <Skeleton height={90} />
      ) : att.error ? (
        <InlineError message={att.error} onRetry={att.reload} />
      ) : (
        <CodeBlock code={jsonText(att.data)} />
      )}
    </Card>
  );
}

function AttVerifyChip({ state }: { state: VerifyState }) {
  if (state.phase === "idle") return <ChipText tone="muted">not verified</ChipText>;
  if (state.phase === "verifying") return <ChipText tone="accent">verifying…</ChipText>;
  if (state.phase === "error") return <ChipText tone="danger">verify failed</ChipText>;

  const r = state.result;
  if (!r.verified) {
    const why = r.mismatches && r.mismatches.length > 0 ? ` · ${r.mismatches.join(", ")}` : "";
    return <ChipText tone="danger">attestation invalid{why}</ChipText>;
  }
  if (r.signature_verified === true) {
    const k = r.signing_key_id ? ` (${r.signing_key_id})` : "";
    return <ChipText tone="success">valid · signed{k}</ChipText>;
  }
  if (r.signature_verified === false)
    return <ChipText tone="danger">signature invalid</ChipText>;
  return <ChipText tone="warning">digest valid · unsigned</ChipText>;
}

function ChipText({ tone, children }: { tone: string; children: React.ReactNode }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        color: toneColor(tone),
      }}
    >
      <StatusDot tone={tone} pulse={false} />
      {children}
    </span>
  );
}

// --------------------------------------------------------------------------
// Evidence
// --------------------------------------------------------------------------

function EvidencePanel({ refId }: { refId: string }) {
  const evidence = useLoad<DeploymentEvidence>(() => getDeploymentEvidence(refId));
  return (
    <Card label="Evidence" pad={14}>
      {evidence.loading && !evidence.data ? (
        <Skeleton height={90} />
      ) : evidence.error ? (
        <InlineError message={evidence.error} onRetry={evidence.reload} />
      ) : (
        <CodeBlock code={jsonText(evidence.data)} />
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Timeline — reuses the Runs node-timeline row style
// --------------------------------------------------------------------------

function TimelinePanel({ refId }: { refId: string }) {
  const timeline = useLoad<AuditTimeline>(() => getDeploymentTimeline(refId));
  return (
    <Card label="Timeline" pad={14}>
      {timeline.loading && !timeline.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} height={22} />
          ))}
        </div>
      ) : timeline.error ? (
        <InlineError message={timeline.error} onRetry={timeline.reload} />
      ) : (timeline.data?.entries ?? []).length === 0 ? (
        <EmptyInline>No timeline entries yet.</EmptyInline>
      ) : (
        <div>
          {(timeline.data?.entries ?? []).map((e) => (
            <TimelineRow key={e.audit_id} rec={e} />
          ))}
        </div>
      )}
    </Card>
  );
}

function TimelineRow({ rec }: { rec: NodeAuditRecord }) {
  const s = rec.status.toLowerCase();
  const tone = NODE_TONE[s] ?? "neutral";
  const running = NODE_RUNNING.has(s);
  const queued = NODE_QUEUED.has(s);
  const type = nodeTypeOf(rec);
  const dur = fmtDuration(rec.started_at, rec.completed_at);
  const note = rec.error ?? (rec.attempt > 1 ? `retry #${rec.attempt}` : "");

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
// Cost
// --------------------------------------------------------------------------

function CostPanel({ refId }: { refId: string }) {
  const cost = useLoad<DeploymentCost>(() => getCostOf(refId));
  return (
    <Card label="Cost" pad={14}>
      {cost.loading && !cost.data ? (
        <Skeleton height={28} width={160} />
      ) : cost.error ? (
        <InlineError message={cost.error} onRetry={cost.reload} />
      ) : cost.data ? (
        <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 22, fontWeight: 600 }}>
            {fmtCost(cost.data.total_cost_usd)}
          </span>
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-faint)" }}>
            {cost.data.currency} · cumulative
          </span>
        </div>
      ) : (
        <EmptyInline>No cost recorded.</EmptyInline>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Audits — the full deployment-scoped chain lives on the Audit screen
// --------------------------------------------------------------------------

function AuditsPanel() {
  return (
    <Card label="Audits" pad={14}>
      <p style={{ margin: "0 0 10px", fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>
        The full, verifiable audit chain for this deployment — every node event,
        digest, and signature — renders on the Audit screen.
      </p>
      <Link href="/audit" style={{ textDecoration: "none" }}>
        <Button variant="neutral">Open Audit →</Button>
      </Link>
    </Card>
  );
}

// --------------------------------------------------------------------------
// Shared bits
// --------------------------------------------------------------------------

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

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ padding: 18, fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>
      {children}
    </div>
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
