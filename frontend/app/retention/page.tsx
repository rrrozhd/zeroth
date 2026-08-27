"use client";

// The Retention & Compliance screen (handoff §9) — a governance view over the
// three retention surfaces this deployment exposes: the tenant retention policy
// (TTLs), legal holds (which freeze expiry/erasure), and right-to-erasure.
//
// Rebuilt on the P0 primitives. Renders ONLY real wire fields (see api-types.ts);
// where the design mock is richer than the API, the API wins:
//
//   RetentionPolicyResponse: { enabled, run_ttl_seconds?, audit_ttl_seconds?,
//     tenant_id }. The mock's four TTL scopes (run payloads / audit / artifacts /
//     thread memory) do NOT all exist — the policy governs exactly two TTLs
//     (run payloads, audit records) plus a master `enabled` flag. We edit those
//     two only. TTLs are stored in SECONDS; the row inputs edit in days for
//     readability and show the resolved value as a mono-teal humanized readout.
//   RetentionPolicyBody (PUT): { enabled, run_ttl_seconds?, audit_ttl_seconds? }.
//   LegalHoldResponse: { hold_id, tenant_id, run_id?, reason?, placed_by?, active }.
//     Active holds are loaded from persistent tenant storage; an omitted run_id
//     places a tenant-wide hold.
//   LegalHoldBody (POST): { run_id?, reason? }.
//   ErasureRequestBody (POST): { run_id? } XOR { tenant_id? } — exactly one. There
//     is no subject/fields/note field; the "note" input below is a LOCAL operator
//     memo, never sent. Erasure executes in one call (no server "pending" state),
//     so the pending → Execute → ERASED flow is a client-side confirm stage over
//     the single requestErasure() call — appropriate, since erasure is irreversible.
//   ErasureResponse: { reason, runs?: ErasureRunResult[] }.
//   ErasureRunResult: { run_id, run_redacted, audits_erased, artifacts_deleted,
//     checkpoints_deleted, econ_events_deleted? }.
//
// Every mutation (save policy, place/release hold, execute erasure) fires a toast.
// The API key lives only in localStorage (lib/config) — never logged, never in a
// URL. Nothing here crashes when the API is unconfigured or unreachable: useLoad
// turns failures into an inline error state, and the mutating cards gate on a
// configured key.

import { useEffect, useRef, useState } from "react";
import {
  Button,
  Card,
  MonoLabel,
  Pill,
  Skeleton,
  StatusDot,
  TONE,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  errMsg,
  getRetentionPolicy,
  listErasureHistory,
  listLegalHolds,
  placeLegalHold,
  putRetentionPolicy,
  releaseLegalHold,
  requestErasure,
  type ErasureResult,
  type ErasureHistoryEntry,
  type LegalHold,
  type RetentionPolicy,
  type RetentionPolicyBody,
} from "@/app/lib/api";
import { getTenant, isConfigured } from "@/app/lib/config";
import { isForbiddenSurface, surfaceAccessMessage } from "@/app/lib/surfaceAccess";

const DAY = 86_400;
const MAX_TTL_SECONDS = 2_147_483_647;
const MAX_TTL_DAYS = MAX_TTL_SECONDS / DAY;

// --------------------------------------------------------------------------
// TTL helpers — the policy stores seconds; the UI edits days and reads back a
// compact humanized string so an odd (non-day) value never lies on screen.
// --------------------------------------------------------------------------

/** Compact "90d", "365d", "1d 6h", "45m", "no expiry". */
function humanizeTtl(sec: number | null | undefined): string {
  if (sec == null) return "no expiry";
  if (sec <= 0) return "0s";
  const d = Math.floor(sec / DAY);
  const h = Math.floor((sec % DAY) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const parts: string[] = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (s) parts.push(`${s}s`);
  return parts.join(" ") || "0s";
}

/** Seconds -> the days string shown in the input (whole when exact). */
function secToDaysField(sec: number | null | undefined): string {
  if (sec == null) return "";
  if (sec % DAY === 0) return String(sec / DAY);
  const precision = sec < DAY ? 10 : 4;
  return String(Number((sec / DAY).toFixed(precision)));
}

/** Days input -> seconds. "" = null (no expiry); values below one second are invalid. */
function daysFieldToSec(s: string): number | null | "err" {
  const t = s.trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n <= 0 || n > MAX_TTL_DAYS) return "err";
  const seconds = Math.round(n * DAY);
  return seconds >= 1 && seconds <= MAX_TTL_SECONDS ? seconds : "err";
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

export default function RetentionPage() {
  // localStorage config is read only after mount so the static prerender and the
  // first client render agree (no hydration mismatch), matching the other screens.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();
  const policy = useLoad<RetentionPolicy>(getRetentionPolicy);
  const accessResolving = !mounted || (connected && policy.loading && !policy.data && !policy.error);
  const accessError = policy.error;

  return (
    <div style={{ maxWidth: 980, margin: "0 auto", padding: "28px 28px 48px" }}>
      <header style={{ marginBottom: 22 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>
          Retention &amp; Compliance
        </h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          Data-lifecycle TTLs, legal holds that freeze expiry, and chain-safe right-to-erasure.
        </p>
      </header>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))",
          gap: 24,
          alignItems: "start",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <RetentionPolicyCard connected={connected} mounted={mounted} policy={policy} />
          <LegalHoldsCard
            connected={connected}
            mounted={mounted}
            accessResolving={accessResolving}
            accessError={accessError}
            reloadAccess={policy.reload}
          />
        </div>
        <ErasureCard
          connected={connected}
          mounted={mounted}
          accessResolving={accessResolving}
          accessError={accessError}
          reloadAccess={policy.reload}
        />
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Retention policy — the two governed TTLs + master enable, editable inline.
// --------------------------------------------------------------------------

function RetentionPolicyCard({
  connected,
  mounted,
  policy,
}: {
  connected: boolean;
  mounted: boolean;
  policy: Loadable<RetentionPolicy>;
}) {
  const toast = useToast();

  const [enabled, setEnabled] = useState(true);
  const [runDays, setRunDays] = useState("");
  const [auditDays, setAuditDays] = useState("");
  const [saving, setSaving] = useState(false);

  // Sync the editable form from the loaded policy (and after every reload).
  useEffect(() => {
    const p = policy.data;
    if (!p) return;
    setEnabled(p.enabled);
    setRunDays(secToDaysField(p.run_ttl_seconds));
    setAuditDays(secToDaysField(p.audit_ttl_seconds));
  }, [policy.data]);

  const runSec = daysFieldToSec(runDays);
  const auditSec = daysFieldToSec(auditDays);
  const valid = runSec !== "err" && auditSec !== "err";

  const p = policy.data;
  const dirty =
    valid &&
    p != null &&
    (enabled !== p.enabled ||
      (runSec as number | null) !== (p.run_ttl_seconds ?? null) ||
      (auditSec as number | null) !== (p.audit_ttl_seconds ?? null));

  async function save() {
    if (!dirty || !valid || saving) return;
    setSaving(true);
    try {
      const body: RetentionPolicyBody = {
        enabled,
        run_ttl_seconds: runSec as number | null,
        audit_ttl_seconds: auditSec as number | null,
      };
      await putRetentionPolicy(body);
      toast(`Retention policy saved${enabled ? "" : " · disabled"}`);
      policy.reload();
    } catch (e) {
      toast(`Save failed: ${errMsg(e)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card pad={16} data-evidence-id="retention.policy.card">
      <SectionHead label="Retention policy">
        {p && <Pill tone="muted">tenant {p.tenant_id}</Pill>}
      </SectionHead>

      {policy.loading && !policy.data ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} height={30} />
          ))}
        </div>
      ) : policy.error ? (
        <InlineError message={policy.error} onRetry={policy.reload} />
      ) : mounted && !connected ? (
        <EmptyInline>Connect to the API (top bar) to load the retention policy.</EmptyInline>
      ) : !p ? (
        <EmptyInline>No retention policy resolved for this tenant.</EmptyInline>
      ) : (
        <>
          {/* master enable */}
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 0",
              borderBottom: "1px solid var(--hair)",
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              aria-label="Retention enforcement enabled"
              data-evidence-id="retention.policy.enabled"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              style={{ accentColor: "var(--accent)", width: 15, height: 15 }}
            />
            <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span style={{ fontSize: 12.5, color: "var(--text-primary)" }}>
                Enforcement {enabled ? "enabled" : "disabled"}
              </span>
              <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
                {enabled
                  ? "Records past their TTL are swept (except while under legal hold)."
                  : "TTLs are retained but not enforced — nothing expires."}
              </span>
            </span>
          </label>

          <div style={{ opacity: enabled ? 1 : 0.55, transition: "opacity 120ms ease" }}>
            <TtlRow
              scope="Run payloads"
              hint="Inputs, outputs, artifacts & checkpoints of a run."
              days={runDays}
              onDays={setRunDays}
              seconds={runSec}
            />
            <TtlRow
              scope="Audit records"
              hint="Tamper-evident per-node audit trail."
              days={auditDays}
              onDays={setAuditDays}
              seconds={auditSec}
            />
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 14 }}>
            <Button
              variant="primary"
              onClick={save}
              disabled={!dirty || !valid || saving}
              data-evidence-id="retention.policy.save"
            >
              {saving ? "Saving…" : "Save policy"}
            </Button>
            {dirty && !saving && (
              <span style={{ fontSize: 11, color: "var(--text-faint)" }}>Unsaved changes</span>
            )}
            {!valid && (
              <span id="retention-policy-ttl-error" role="alert" style={{ fontSize: 11, color: "var(--danger)" }}>
                TTL must resolve to 1–2147483647 seconds (max 24855.1348 days; blank = no expiry).
              </span>
            )}
          </div>
        </>
      )}
    </Card>
  );
}

/** One editable TTL row: scope + days input + mono-teal resolved readout. */
function TtlRow({
  scope,
  hint,
  days,
  onDays,
  seconds,
}: {
  scope: string;
  hint: string;
  days: string;
  onDays: (v: string) => void;
  seconds: number | null | "err";
}) {
  const err = seconds === "err";
  const inputId = `retention-${scope.toLowerCase().replaceAll(" ", "-")}-ttl-days`;
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(0,1fr) 132px",
        gap: 12,
        alignItems: "center",
        padding: "12px 0",
        borderBottom: "1px solid var(--hair)",
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 12.5, color: "var(--text-primary)" }}>{scope}</div>
        <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2 }}>{hint}</div>
        <div
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11.5,
            color: err ? "var(--danger)" : "var(--accent)",
            marginTop: 5,
          }}
        >
          {err ? "invalid" : humanizeTtl(seconds as number | null)}
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <input
          id={inputId}
          aria-label={`${scope} TTL in days`}
          data-evidence-id={`retention.policy.${scope.toLowerCase().replaceAll(" ", "-")}-ttl`}
          aria-invalid={err}
          aria-describedby={err ? "retention-policy-ttl-error" : undefined}
          value={days}
          onChange={(e) => onDays(e.target.value)}
          placeholder="∞"
          inputMode="decimal"
          min={1 / DAY}
          style={{
            width: "100%",
            boxSizing: "border-box",
            fontFamily: "var(--font-mono)",
            fontSize: 12.5,
            textAlign: "right",
            color: "var(--text-primary)",
            background: "var(--bg-card)",
            border: `1px solid ${err ? "var(--danger)" : "var(--hair-strong)"}`,
            borderRadius: 6,
            padding: "7px 9px",
            outline: "none",
          }}
        />
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--text-faint)" }}>
          d
        </span>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Legal holds — persistent tenant-scoped list, place + release.
// Amber-tinted: a hold suspends TTL expiry & erasure for its scope.
// --------------------------------------------------------------------------

function LegalHoldsCard({
  connected,
  mounted,
  accessResolving,
  accessError,
  reloadAccess,
}: {
  connected: boolean;
  mounted: boolean;
  accessResolving: boolean;
  accessError: string | null;
  reloadAccess: () => void;
}) {
  const toast = useToast();
  const holds = useLoad<LegalHold[]>(listLegalHolds);
  const [runId, setRunId] = useState("");
  const [reason, setReason] = useState("");
  const [placing, setPlacing] = useState(false);
  const [releasing, setReleasing] = useState<string | null>(null);

  async function place(e: React.FormEvent) {
    e.preventDefault();
    if (placing) return;
    setPlacing(true);
    try {
      const hold = await placeLegalHold({
        run_id: runId.trim() || null,
        reason: reason.trim() || null,
      });
      toast(`Legal hold placed · ${hold.hold_id}`);
      setRunId("");
      setReason("");
      holds.reload();
    } catch (err) {
      toast(`Place hold failed: ${errMsg(err)}`);
    } finally {
      setPlacing(false);
    }
  }

  async function release(holdId: string) {
    if (!window.confirm(`Release legal hold ${holdId}? Protected data may become erasable.`)) return;
    setReleasing(holdId);
    try {
      await releaseLegalHold(holdId);
      toast(`Legal hold released · ${holdId}`);
      holds.reload();
    } catch (err) {
      toast(`Release failed: ${errMsg(err)}`);
      setReleasing(null);
    }
  }

  const amber = "var(--warning)";

  return (
    <Card
      pad={16}
      data-evidence-id="retention.legal-holds.card"
      style={{
        border: `1px solid color-mix(in srgb, ${amber} 32%, transparent)`,
        background: `color-mix(in srgb, ${amber} 6%, var(--bg-card))`,
      }}
    >
      <SectionHead label="Legal holds">
        <Pill tone="warning">freezes expiry</Pill>
      </SectionHead>

      <p style={{ margin: "0 0 12px", fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.55 }}>
        A hold suspends TTL sweeps and erasure for its scope until released. Active holds are
        persisted and reloaded from tenant-scoped storage.
      </p>

      {mounted && !connected ? (
        <EmptyInline>Connect to the API (top bar) to place legal holds.</EmptyInline>
      ) : accessResolving ? (
        <Skeleton height={54} />
      ) : accessError ? (
        <InlineError message={accessError} onRetry={reloadAccess} />
      ) : holds.loading && !holds.data ? (
        <Skeleton height={54} />
      ) : holds.error ? (
        <InlineError message={holds.error} onRetry={holds.reload} />
      ) : (
        <>
          {(holds.data?.length ?? 0) === 0 ? (
            <EmptyInline>No active legal holds.</EmptyInline>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 14 }}>
              {holds.data?.map((h) => (
                <div
                  key={h.hold_id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "10px 12px",
                    borderRadius: 8,
                    border: `1px solid color-mix(in srgb, ${amber} 26%, transparent)`,
                    background: `color-mix(in srgb, ${amber} 8%, transparent)`,
                  }}
                >
                  <div style={{ minWidth: 0, flex: 1, display: "flex", flexDirection: "column", gap: 4 }}>
                    <span
                      title={h.hold_id}
                      style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: 12,
                        fontWeight: 600,
                        color: "var(--text-primary)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {h.hold_id}
                    </span>
                    <span style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                      <ScopeTag>{h.run_id ? `run ${h.run_id}` : "tenant-wide"}</ScopeTag>
                      <Pill tone="warning">TTLs suspended</Pill>
                    </span>
                    {h.reason && (
                      <span style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>
                        {h.reason}
                      </span>
                    )}
                  </div>
                  <Button
                    onClick={() => release(h.hold_id)}
                    disabled={releasing === h.hold_id}
                    aria-label={`Release legal hold ${h.hold_id}`}
                    data-evidence-id={`retention.legal-holds.release.${h.hold_id}`}
                    style={{ padding: "5px 9px", flexShrink: 0 }}
                  >
                    {releasing === h.hold_id ? "…" : "Release"}
                  </Button>
                </div>
              ))}
            </div>
          )}

          <form onSubmit={place} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <Field label="run_id" hint="Blank places a tenant-wide hold (freezes every run).">
              <TextInput
                value={runId}
                onChange={setRunId}
                placeholder="run_… (optional)"
                ariaLabel="Legal hold run ID"
                evidenceId="retention.legal-holds.run-id"
              />
            </Field>
            <Field label="reason" hint="Recorded on the hold for the audit trail.">
              <TextInput
                value={reason}
                onChange={setReason}
                placeholder="e.g. litigation ref #4821"
                ariaLabel="Legal hold reason"
                evidenceId="retention.legal-holds.reason"
              />
            </Field>
            <div>
              <Button
                type="submit"
                variant="primary"
                disabled={placing}
                data-evidence-id="retention.legal-holds.place"
              >
                {placing ? "Placing…" : "Place hold"}
              </Button>
            </div>
          </form>
        </>
      )}
    </Card>
  );
}

// --------------------------------------------------------------------------
// Erasure — stage a request, then Execute (irreversible) → requestErasure().
// The API executes in one call; the pending stage is a client-side confirm.
// --------------------------------------------------------------------------

type Scope = "run" | "tenant";

type ErasureItem = {
  localId: string;
  scope: Scope;
  runId: string;
  tenantId: string;
  note: string; // local memo — never sent (no such API field)
  status: "pending" | "erasing" | "erased" | "error";
  result?: ErasureResult;
  error?: string;
};

function ErasureCard({
  connected,
  mounted,
  accessResolving,
  accessError,
  reloadAccess,
}: {
  connected: boolean;
  mounted: boolean;
  accessResolving: boolean;
  accessError: string | null;
  reloadAccess: () => void;
}) {
  const toast = useToast();
  const history = useLoad<ErasureHistoryEntry[]>(() => listErasureHistory(25));
  const seq = useRef(0);
  const [items, setItems] = useState<ErasureItem[]>([]);

  const [scope, setScope] = useState<Scope>("run");
  const [runId, setRunId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [note, setNote] = useState("");

  // Default the tenant field to the configured tenant once mounted.
  useEffect(() => {
    if (mounted) setTenantId((t) => t || getTenant());
  }, [mounted]);

  const canStage =
    scope === "run" ? runId.trim().length > 0 : tenantId.trim().length > 0;

  function stage(e: React.FormEvent) {
    e.preventDefault();
    if (!canStage) return;
    const id = `ER-${++seq.current}`;
    setItems((xs) => [
      {
        localId: id,
        scope,
        runId: runId.trim(),
        tenantId: tenantId.trim(),
        note: note.trim(),
        status: "pending",
      },
      ...xs,
    ]);
    setRunId("");
    setNote("");
    // keep tenantId (usually stable) for the next request
  }

  function discard(localId: string) {
    setItems((xs) => xs.filter((x) => x.localId !== localId));
  }

  async function execute(item: ErasureItem) {
    if (
      typeof window !== "undefined" &&
      !window.confirm(
        item.scope === "run"
          ? `Execute erasure of run ${item.runId}? This is irreversible.`
          : `Execute erasure of every non-held run for tenant ${item.tenantId}? This is irreversible.`,
      )
    ) {
      return;
    }
    setItems((xs) =>
      xs.map((x) => (x.localId === item.localId ? { ...x, status: "erasing" } : x)),
    );
    try {
      const result = await requestErasure(
        item.scope === "run" ? { run_id: item.runId } : { tenant_id: item.tenantId },
      );
      setItems((xs) =>
        xs.map((x) =>
          x.localId === item.localId ? { ...x, status: "erased", result } : x,
        ),
      );
      const n = result.runs?.length ?? 0;
      toast(`Erased ${n} run${n === 1 ? "" : "s"} · chain-safe: audit tombstones preserve digest continuity`);
      history.reload();
    } catch (e) {
      const msg = errMsg(e);
      setItems((xs) =>
        xs.map((x) =>
          x.localId === item.localId ? { ...x, status: "error", error: msg } : x,
        ),
      );
      // 409 = a legal hold blocks the erasure; surface it verbatim.
      toast(`Erasure failed: ${msg}`);
      history.reload();
    }
  }

  return (
    <Card pad={16} data-evidence-id="retention.erasure.card">
      <SectionHead label="Erasure requests">
        <Pill tone="accent">right-to-erasure</Pill>
      </SectionHead>

      {mounted && !connected ? (
        <EmptyInline>Connect to the API (top bar) to request erasure.</EmptyInline>
      ) : accessResolving ? (
        <Skeleton height={120} />
      ) : accessError ? (
        <InlineError message={accessError} onRetry={reloadAccess} />
      ) : (
        <>
          <form onSubmit={stage} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <Field label="scope" hint="Erase a single run, or every non-held run of a tenant.">
              <div style={{ display: "flex", gap: 6 }}>
                <ScopeToggle
                  active={scope === "run"}
                  onClick={() => setScope("run")}
                  ariaLabel="single run"
                  evidenceId="retention.erasure.scope.run"
                >
                  single run
                </ScopeToggle>
                <ScopeToggle
                  active={scope === "tenant"}
                  onClick={() => setScope("tenant")}
                  ariaLabel="entire tenant"
                  evidenceId="retention.erasure.scope.tenant"
                >
                  entire tenant
                </ScopeToggle>
              </div>
            </Field>

            {scope === "run" ? (
              <Field label="run_id" hint="The run whose payloads & audit records to erase.">
                <TextInput
                  value={runId}
                  onChange={setRunId}
                  placeholder="run_…"
                  autoFocus
                  evidenceId="retention.erasure.run-id"
                />
              </Field>
            ) : (
              <Field label="tenant_id" hint="Erases every run not under legal hold.">
                <TextInput
                  value={tenantId}
                  onChange={setTenantId}
                  placeholder="default"
                  evidenceId="retention.erasure.tenant-id"
                />
              </Field>
            )}

            <Field label="note" hint="Local memo — attached to this card only, never sent to the API.">
              <TextInput
                value={note}
                onChange={setNote}
                placeholder="e.g. DSAR-2026-114 (optional)"
                evidenceId="retention.erasure.note"
              />
            </Field>

            <div>
              <Button
                type="submit"
                variant="neutral"
                disabled={!canStage}
                data-evidence-id="retention.erasure.stage"
              >
                Stage erasure request
              </Button>
            </div>
          </form>

          {items.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 16 }}>
              {items.map((item) => (
                <ErasureRow
                  key={item.localId}
                  item={item}
                  onExecute={() => execute(item)}
                  onDiscard={() => discard(item.localId)}
                />
              ))}
            </div>
          )}

          {history.data && history.data.length > 0 && (
            <div
              data-evidence-id="retention.erasure.history"
              style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 16 }}
            >
              <div style={{ fontSize: 11, color: "var(--text-faint)" }}>Recent durable activity</div>
              {history.data.slice(0, 8).map((entry) => (
                <div
                  key={entry.log_id}
                  data-evidence-id={`retention.erasure.history.${entry.log_id}`}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1fr) auto",
                    gap: 8,
                    padding: "8px 10px",
                    border: "1px solid var(--hair)",
                    borderRadius: 8,
                    background: "var(--bg-raised)",
                    fontSize: 11,
                  }}
                >
                  <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {entry.action} · {entry.run_id ?? "tenant"}
                  </span>
                  <span style={{ color: "var(--text-faint)" }}>
                    {new Date(entry.created_at).toLocaleString()}
                  </span>
                </div>
              ))}
            </div>
          )}

          <p
            style={{
              margin: "16px 0 0",
              paddingTop: 14,
              borderTop: "1px solid var(--hair)",
              fontSize: 11,
              color: "var(--text-faint)",
              lineHeight: 1.6,
            }}
          >
            <b style={{ color: "var(--text-muted)" }}>Chain-safe crypto-erasure.</b> Run payloads,
            artifacts & checkpoints are hard-deleted and the run row redacted; audit records are
            cryptographically erased in place (tombstoned) so the tamper-evident hash chain still
            verifies end-to-end. Runs under a legal hold are refused (409).
          </p>
        </>
      )}
    </Card>
  );
}

function ErasureRow({
  item,
  onExecute,
  onDiscard,
}: {
  item: ErasureItem;
  onExecute: () => void;
  onDiscard: () => void;
}) {
  const subject =
    item.scope === "run" ? `run ${item.runId}` : `tenant ${item.tenantId} · all non-held runs`;

  const statusTone =
    item.status === "erased"
      ? "success"
      : item.status === "error"
        ? "danger"
        : item.status === "erasing"
          ? "accent"
          : "warning";
  const statusLabel =
    item.status === "erased"
      ? "ERASED"
      : item.status === "error"
        ? "FAILED"
        : item.status === "erasing"
          ? "erasing…"
          : "pending";

  return (
    <div
      data-evidence-id={`retention.erasure.item.${item.localId}`}
      style={{
        padding: "12px 14px",
        borderRadius: 8,
        border: "1px solid var(--hair)",
        background: "var(--bg-raised)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11.5,
            fontWeight: 600,
            color: "var(--text-muted)",
          }}
        >
          {item.localId}
        </span>
        <span
          title={subject}
          style={{
            flex: 1,
            minWidth: 0,
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            color: "var(--text-primary)",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {subject}
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
          <StatusDot tone={statusTone} pulse={item.status === "erasing"} />
          <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: TONE[statusTone] }}>
            {statusLabel}
          </span>
        </span>
      </div>

      {item.note && (
        <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5 }}>
          {item.note}
        </div>
      )}

      {item.status === "error" && item.error && (
        <div style={{ marginTop: 6, fontSize: 11.5, color: "var(--danger)", lineHeight: 1.5 }}>
          {item.error}
        </div>
      )}

      {item.status === "erased" && item.result && <ErasureResultView result={item.result} />}

      {(item.status === "pending" || item.status === "erasing" || item.status === "error") && (
        <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
          <Button
            variant="primary"
            onClick={onExecute}
            disabled={item.status === "erasing"}
            data-evidence-id={`retention.erasure.execute.${item.localId}`}
            style={{ padding: "5px 10px" }}
          >
            {item.status === "erasing"
              ? "Erasing…"
              : item.status === "error"
                ? "Retry erasure"
                : "Execute erasure"}
          </Button>
          {item.status !== "erasing" && (
            <Button
              variant="neutral"
              onClick={onDiscard}
              data-evidence-id={`retention.erasure.discard.${item.localId}`}
              style={{ padding: "5px 10px" }}
            >
              Discard
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/** The per-run counts returned by a completed erasure. */
function ErasureResultView({ result }: { result: ErasureResult }) {
  const runs = result.runs ?? [];
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginBottom: 8 }}>
        reason: <span style={{ fontFamily: "var(--font-mono)" }}>{result.reason}</span>
      </div>
      {runs.length === 0 ? (
        <div style={{ fontSize: 11.5, color: "var(--text-faint)" }}>No runs matched.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {runs.map((r) => (
            <div
              key={r.run_id}
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: 6,
                paddingTop: 8,
                borderTop: "1px solid var(--hair)",
              }}
            >
              <span
                title={r.run_id}
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  color: "var(--text-secondary)",
                  maxWidth: 180,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {r.run_id}
              </span>
              <Pill tone={r.run_redacted ? "success" : "muted"}>
                {r.run_redacted ? "redacted" : "not redacted"}
              </Pill>
              <Metric label="audits" value={r.audits_erased} />
              <Metric label="artifacts" value={r.artifacts_deleted} />
              <Metric label="checkpoints" value={r.checkpoints_deleted} />
              {r.econ_events_deleted != null && (
                <Metric label="econ" value={r.econ_events_deleted} />
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Shared bits (mirror the Connectors / Templates conventions)
// --------------------------------------------------------------------------

function SectionHead({ label, children }: { label: string; children?: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 8,
        marginBottom: 12,
      }}
    >
      <MonoLabel>{label}</MonoLabel>
      {children}
    </div>
  );
}

/** A small factual count chip, e.g. "audits 3". */
function Metric({ label, value }: { label: string; value: number }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-sans)",
        fontSize: 10.5,
        color: "var(--text-secondary)",
        background: "var(--bg-raised)",
        border: "1px solid var(--hair)",
        borderRadius: 5,
        padding: "1px 6px",
      }}
    >
      {label} <b style={{ color: "var(--text-primary)" }}>{value}</b>
    </span>
  );
}

/** A neutral mono scope tag (run / tenant-wide). */
function ScopeTag({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 10.5,
        color: "var(--text-secondary)",
        background: "color-mix(in srgb, var(--text-faint) 12%, transparent)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 5,
        padding: "2px 7px",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

/** A two-state segmented toggle button for the erasure scope. */
function ScopeToggle({
  active,
  onClick,
  ariaLabel,
  evidenceId,
  children,
}: {
  active: boolean;
  onClick: () => void;
  ariaLabel: string;
  evidenceId: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      aria-pressed={active}
      data-evidence-id={evidenceId}
      style={{
        flex: 1,
        fontFamily: "var(--font-sans)",
        fontSize: 12,
        fontWeight: 500,
        padding: "7px 10px",
        borderRadius: 6,
        cursor: "pointer",
        color: active ? "var(--accent)" : "var(--text-secondary)",
        background: active ? "color-mix(in srgb, var(--accent) 12%, transparent)" : "transparent",
        border: `1px solid ${active ? "transparent" : "var(--hair-strong)"}`,
        transition: "background 120ms ease, color 120ms ease",
      }}
    >
      {children}
    </button>
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
  ariaLabel,
  evidenceId,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  ariaLabel?: string;
  evidenceId?: string;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      aria-label={ariaLabel}
      data-evidence-id={evidenceId}
      autoComplete="off"
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: "var(--font-mono)",
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-card)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "8px 10px",
        outline: "none",
      }}
    />
  );
}

function EmptyInline({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>{children}</div>;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  const restricted = isForbiddenSurface(message);
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
        {surfaceAccessMessage(message, "Retention controls")}
      </span>
      {!restricted && (
        <Button variant="danger" onClick={onRetry} style={{ flexShrink: 0 }}>
          Retry
        </Button>
      )}
    </div>
  );
}
