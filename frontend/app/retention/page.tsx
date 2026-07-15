"use client";

import { useEffect, useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  ErrorBox,
  Field,
  Input,
  Json,
  Mono,
  NotConnected,
  Skeleton,
  PageHeader,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import {
  errMsg,
  getRetentionPolicy,
  placeLegalHold,
  releaseLegalHold,
  requestErasure,
  updateRetentionPolicy,
  type ErasureResult,
  type LegalHold,
} from "@/app/lib/api";

export default function RetentionPage() {
  const connected = useConnected();
  return (
    <div className="space-y-6">
      <PageHeader
        title="Retention & compliance"
        subtitle="Data-retention policy, legal holds, and right-to-erasure (GDPR / EU AI Act)."
      />
      {!connected ? (
        <NotConnected />
      ) : (
        <>
          <PolicyCard />
          <LegalHoldsCard />
          <ErasureCard />
        </>
      )}
    </div>
  );
}

// Seconds -> human hint, e.g. "= 30 days". Null means "no TTL (keep forever)".
function daysHint(seconds: number | null | undefined): string {
  if (seconds == null || seconds <= 0) return "no TTL — kept indefinitely";
  const days = seconds / 86400;
  return `= ${days >= 1 ? `${Math.round(days)} day${days === 1 ? "" : "s"}` : `${seconds}s`}`;
}

function PolicyCard() {
  const { data, error, loading, reload } = useAsync(getRetentionPolicy, []);
  const [enabled, setEnabled] = useState(false);
  const [runTtl, setRunTtl] = useState("");
  const [auditTtl, setAuditTtl] = useState("");
  const [busy, setBusy] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // Seed the form from the loaded policy (and re-seed after a save reload).
  useEffect(() => {
    if (!data) return;
    setEnabled(data.enabled);
    setRunTtl(data.run_ttl_seconds != null ? String(data.run_ttl_seconds) : "");
    setAuditTtl(data.audit_ttl_seconds != null ? String(data.audit_ttl_seconds) : "");
  }, [data]);

  async function save() {
    setBusy(true);
    setSaveErr(null);
    setSaved(false);
    try {
      const updated = await updateRetentionPolicy({
        enabled,
        run_ttl_seconds: runTtl.trim() ? Number(runTtl) : null,
        audit_ttl_seconds: auditTtl.trim() ? Number(auditTtl) : null,
      });
      setEnabled(updated.enabled);
      setSaved(true);
      reload();
    } catch (e) {
      setSaveErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Retention policy">
      {error && <ApiErrorNote error={error} />}
      {loading && !data && <Skeleton rows={3} />}
      {data && (
        <div className="space-y-4">
          <p className="text-sm text-muted">
            Tenant <Mono>{data.tenant_id}</Mono>. When purge is enabled, the retention worker
            deletes runs and audit records past their TTL — legal holds override it.
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="h-4 w-4"
            />
            <span className="font-medium">Enable scheduled purge</span>
          </label>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Run TTL (seconds)" hint={daysHint(runTtl.trim() ? Number(runTtl) : null)}>
              <Input
                type="number"
                min={0}
                value={runTtl}
                onChange={(e) => setRunTtl(e.target.value)}
                placeholder="none"
                className="font-mono"
              />
            </Field>
            <Field
              label="Audit TTL (seconds)"
              hint={daysHint(auditTtl.trim() ? Number(auditTtl) : null)}
            >
              <Input
                type="number"
                min={0}
                value={auditTtl}
                onChange={(e) => setAuditTtl(e.target.value)}
                placeholder="none"
                className="font-mono"
              />
            </Field>
          </div>
          {saveErr && <ErrorBox message={saveErr} />}
          <div className="flex items-center gap-3">
            <Button variant="primary" onClick={save} disabled={busy}>
              {busy ? "Saving…" : "Save policy"}
            </Button>
            {saved && (
              <span className="text-xs text-emerald-600 dark:text-emerald-400">Saved ✓</span>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

function LegalHoldsCard() {
  const [reason, setReason] = useState("");
  const [runId, setRunId] = useState("");
  const [placing, setPlacing] = useState(false);
  const [placed, setPlaced] = useState<LegalHold | null>(null);
  const [placeErr, setPlaceErr] = useState<string | null>(null);

  const [releaseId, setReleaseId] = useState("");
  const [releasing, setReleasing] = useState(false);
  const [released, setReleased] = useState<string | null>(null);
  const [releaseErr, setReleaseErr] = useState<string | null>(null);

  async function place() {
    setPlacing(true);
    setPlaceErr(null);
    setPlaced(null);
    try {
      const hold = await placeLegalHold({
        reason: reason.trim() || null,
        run_id: runId.trim() || null,
      });
      setPlaced(hold);
      setReason("");
      setRunId("");
    } catch (e) {
      setPlaceErr(errMsg(e));
    } finally {
      setPlacing(false);
    }
  }

  async function release() {
    if (!releaseId.trim()) return;
    setReleasing(true);
    setReleaseErr(null);
    setReleased(null);
    try {
      const hold = await releaseLegalHold(releaseId.trim());
      setReleased(hold.hold_id);
      setReleaseId("");
    } catch (e) {
      setReleaseErr(errMsg(e));
    } finally {
      setReleasing(false);
    }
  }

  return (
    <Card title="Legal holds">
      <div className="space-y-5">
        <p className="text-sm text-muted">
          A legal hold blocks erasure and scheduled purge for a specific run (or the whole
          tenant when no run is given) — for litigation or investigation.
        </p>

        <div className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted">
            Place a hold
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Run ID" hint="optional — tenant-wide if empty">
              <Input value={runId} onChange={(e) => setRunId(e.target.value)} className="font-mono" />
            </Field>
            <Field label="Reason" hint="optional">
              <Input value={reason} onChange={(e) => setReason(e.target.value)} />
            </Field>
          </div>
          {placeErr && <ErrorBox message={placeErr} />}
          {placed && (
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              Hold placed — id <Mono>{placed.hold_id}</Mono> (keep this to release it later).
            </p>
          )}
          <Button variant="primary" onClick={place} disabled={placing}>
            {placing ? "Placing…" : "Place hold"}
          </Button>
        </div>

        <div className="space-y-3 border-t border-border pt-4">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted">
            Release a hold
          </div>
          <div className="flex items-end gap-2">
            <Field label="Hold ID">
              <Input
                value={releaseId}
                onChange={(e) => setReleaseId(e.target.value)}
                className="w-72 font-mono"
              />
            </Field>
            <Button onClick={release} disabled={releasing || !releaseId.trim()}>
              {releasing ? "Releasing…" : "Release"}
            </Button>
          </div>
          {releaseErr && <ErrorBox message={releaseErr} />}
          {released && (
            <p className="text-sm text-emerald-700 dark:text-emerald-400">
              Released hold <Mono>{released}</Mono>.
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}

function ErasureCard() {
  const [runId, setRunId] = useState("");
  const [tenantWide, setTenantWide] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ErasureResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await requestErasure({
        run_id: tenantWide ? null : runId.trim() || null,
        tenant_id: null,
      });
      setResult(res);
      setConfirm(false);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  const ready = tenantWide || runId.trim().length > 0;

  return (
    <Card title="Right to erasure">
      <div className="space-y-4">
        <p className="text-sm text-muted">
          Irreversibly redacts a run&apos;s data (audit records, artifacts, checkpoints, econ
          events). A legal hold blocks this with a <Mono>409</Mono>. This cannot be undone.
        </p>
        <Field label="Run ID" hint="the run to erase">
          <Input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            disabled={tenantWide}
            className="font-mono"
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={tenantWide}
            onChange={(e) => setTenantWide(e.target.checked)}
            className="h-4 w-4"
          />
          <span>Erase <strong>all</strong> runs for this tenant</span>
        </label>

        {error && <ErrorBox message={error} />}

        {!confirm ? (
          <Button variant="danger" onClick={() => setConfirm(true)} disabled={!ready}>
            Request erasure…
          </Button>
        ) : (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-red-300 bg-red-50 p-3 dark:border-red-900/60 dark:bg-red-950/40">
            <span className="text-sm text-red-700 dark:text-red-300">
              {tenantWide
                ? "Erase every run for this tenant? This is irreversible."
                : `Erase run ${runId.trim()}? This is irreversible.`}
            </span>
            <Button variant="danger" onClick={submit} disabled={busy}>
              {busy ? "Erasing…" : "Confirm erasure"}
            </Button>
            <Button variant="ghost" onClick={() => setConfirm(false)} disabled={busy}>
              Cancel
            </Button>
          </div>
        )}

        {result && (
          <div className="space-y-2">
            <p className="text-sm">
              Erased {result.runs?.length ?? 0} run{result.runs?.length === 1 ? "" : "s"} —
              reason: <Mono>{result.reason}</Mono>
            </p>
            <Json value={result.runs ?? []} />
          </div>
        )}
      </div>
    </Card>
  );
}
