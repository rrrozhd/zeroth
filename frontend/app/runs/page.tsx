"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ApiErrorNote,
  buildRunCurl,
  Button,
  Card,
  CurlBlock,
  Empty,
  ErrorBox,
  Field,
  Input,
  Json,
  Mono,
  NotConnected,
  PageHeader,
  Skeleton,
  StatusBadge,
  Textarea,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import { getApiKey } from "@/app/lib/config";
import {
  attachQualityVerdict,
  cancelRun,
  type ContractSchema,
  errMsg,
  getInputContract,
  getRun,
  getRunAuditVerification,
  getRunTimeline,
  interruptRun,
  listRuns,
  replayRun,
  submitRun,
  type AuditVerification,
  type RunStatus,
} from "@/app/lib/api";

const ACTIVE = new Set([
  "running",
  "pending",
  "queued",
  "in_progress",
  "paused",
  "awaiting_approval",
]);

export default function RunsPage() {
  const connected = useConnected();
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    setSelected(new URLSearchParams(window.location.search).get("run_id"));
  }, []);

  // Keep the URL in sync so run details are deep-linkable and reload-safe.
  function select(id: string | null) {
    setSelected(id);
    window.history.replaceState(
      null,
      "",
      id ? `?run_id=${id}` : window.location.pathname,
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Runs" subtitle="Submit and inspect runs." />
      {!connected ? (
        <NotConnected />
      ) : selected ? (
        <RunDetail runId={selected} onBack={() => select(null)} />
      ) : (
        <>
          <SubmitRun onSubmitted={select} />
          <RunList onSelect={select} />
        </>
      )}
    </div>
  );
}

function RunList({ onSelect }: { onSelect: (id: string) => void }) {
  const { data, error, loading, reload } = useAsync(listRuns, []);
  return (
    <Card
      title="Recent runs"
      actions={
        <Button onClick={() => reload()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      }
    >
      {error && <ApiErrorNote error={error} />}
      {loading && !data && <Skeleton rows={4} />}
      {data && data.runs.length === 0 && (
        <Empty>No runs yet — submit one above to see it appear here.</Empty>
      )}
      {data && data.runs.length > 0 && (
        <ul className="divide-y divide-border">
          {data.runs.map((r) => (
            <li key={r.run_id}>
              <button
                onClick={() => onSelect(r.run_id)}
                className="-mx-2 flex w-full items-center justify-between gap-3 rounded-lg px-2 py-2.5 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800/40"
              >
                <span className="truncate font-mono text-xs text-muted">{r.run_id}</span>
                <span className="flex shrink-0 items-center gap-3 text-xs text-muted">
                  {r.current_step && <span>{r.current_step}</span>}
                  <StatusBadge status={r.status} />
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// Ready-made payloads to fill the form with — the real shape is defined by the
// deployed graph's input contract, these just show the common patterns.
const PAYLOAD_EXAMPLES: { label: string; payload: Record<string, unknown> }[] = [
  { label: "Question", payload: { question: "What is Zeroth?" } },
  {
    label: "Document",
    payload: {
      document: "Paste the text to process here.",
      instructions: "Summarize the key points.",
    },
  },
  {
    label: "Structured task",
    payload: { task: "generate_report", parameters: { period: "2026-Q2" } },
  },
];

// Build a skeleton object from a JSON Schema's top-level properties, so operators
// start from the deployment's real input shape rather than a guessed example.
function skeletonFromSchema(schema: unknown): Record<string, unknown> {
  const s = schema as { properties?: Record<string, { type?: string; default?: unknown }> };
  const props = s?.properties;
  if (!props || typeof props !== "object") return {};
  const out: Record<string, unknown> = {};
  for (const [key, spec] of Object.entries(props)) {
    if (spec && "default" in spec) {
      out[key] = spec.default;
      continue;
    }
    switch (spec?.type) {
      case "string": out[key] = ""; break;
      case "number":
      case "integer": out[key] = 0; break;
      case "boolean": out[key] = false; break;
      case "array": out[key] = []; break;
      case "object": out[key] = {}; break;
      default: out[key] = null;
    }
  }
  return out;
}

function SubmitRun({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const [payload, setPayload] = useState('{\n  "question": "What is Zeroth?"\n}');
  const [thread, setThread] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The deployment's pinned input contract, if it has one and the key may read it.
  const [contract, setContract] = useState<ContractSchema | null>(null);

  useEffect(() => {
    let cancelled = false;
    getInputContract()
      .then((c) => !cancelled && setContract(c))
      .catch(() => !cancelled && setContract(null));
    return () => {
      cancelled = true;
    };
  }, []);

  // Names the contract requires but the current payload is missing — a soft,
  // client-side hint; the backend validates authoritatively on submit.
  const missingRequired = (() => {
    const req = (contract?.json_schema as { required?: string[] } | undefined)?.required;
    if (!req?.length) return [];
    try {
      const parsed = JSON.parse(payload) as Record<string, unknown>;
      return req.filter((k) => !(k in parsed));
    } catch {
      return [];
    }
  })();

  async function submit() {
    setError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(payload);
    } catch {
      setError("Input payload is not valid JSON.");
      return;
    }
    setBusy(true);
    try {
      const run = await submitRun({
        input_payload: parsed,
        thread_id: thread.trim() || null,
      });
      onSubmitted(run.run_id);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Submit a run">
      <div className="space-y-3">
        {error && <ErrorBox message={error} />}
        <Field label="Input payload (JSON)" hint="the shape is set by your graph's input contract">
          <Textarea
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
            rows={5}
            className="font-mono text-xs"
          />
        </Field>
        {missingRequired.length > 0 && (
          <p className="text-xs text-amber-700 dark:text-amber-400">
            Payload is missing contract-required field
            {missingRequired.length === 1 ? "" : "s"}:{" "}
            <span className="font-mono">{missingRequired.join(", ")}</span>
          </p>
        )}
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <span>Examples:</span>
          {PAYLOAD_EXAMPLES.map((ex) => (
            <button
              key={ex.label}
              type="button"
              onClick={() => setPayload(JSON.stringify(ex.payload, null, 2))}
              className="rounded-full border border-border px-2.5 py-0.5 transition-colors hover:border-accent/40 hover:text-accent"
            >
              {ex.label}
            </button>
          ))}
          {contract && (
            <button
              type="button"
              onClick={() =>
                setPayload(JSON.stringify(skeletonFromSchema(contract.json_schema), null, 2))
              }
              className="rounded-full border border-accent/40 px-2.5 py-0.5 font-medium text-accent transition-colors hover:bg-accent/[0.06]"
            >
              Prefill from contract
            </button>
          )}
        </div>
        {contract && (
          <details>
            <summary className="cursor-pointer text-xs font-medium text-muted transition-colors hover:text-foreground">
              Input contract — {contract.name} v{contract.version}
            </summary>
            <div className="mt-2">
              <Json value={contract.json_schema} />
            </div>
          </details>
        )}
        <Field label="Thread ID" hint="optional">
          <Input
            value={thread}
            onChange={(e) => setThread(e.target.value)}
            className="font-mono"
          />
        </Field>
        <Button variant="primary" onClick={submit} disabled={busy}>
          {busy ? "Submitting…" : "Submit run"}
        </Button>
        {/* The same invocation as a shell command — the deployed graph IS an
            API service; this is how anything outside the console calls it. */}
        <details>
          <summary className="cursor-pointer text-xs font-medium text-muted transition-colors hover:text-foreground">
            Call this API with cURL
          </summary>
          <div className="mt-2">
            <CurlBlock command={buildRunCurl(payload, thread)} secret={getApiKey() || undefined} />
          </div>
        </details>
      </div>
    </Card>
  );
}

function RunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const { data, error, loading, reload } = useAsync<RunStatus>(() => getRun(runId), [runId]);
  const [timeline, setTimeline] = useState<unknown>(null);
  const [tlError, setTlError] = useState<string | null>(null);

  // Poll (in the background, so the Refresh button doesn't flicker) while active.
  const active = data ? ACTIVE.has(data.status.toLowerCase()) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => reload(true), 1500);
    return () => clearInterval(t);
  }, [active, reload]);

  async function loadTimeline() {
    setTlError(null);
    try {
      setTimeline(await getRunTimeline(runId));
    } catch (e) {
      setTlError(errMsg(e));
    }
  }

  return (
    <div className="space-y-4">
      <button onClick={onBack} className="text-sm text-muted hover:underline">
        ← Back to runs
      </button>

      <Card
        title={<Mono>{runId}</Mono>}
        actions={
          <div className="flex items-center gap-2">
            {data && (
              <RunControls runId={runId} status={data.status} onChanged={() => reload()} />
            )}
            <Button onClick={() => reload()} disabled={loading}>
              {loading ? "Loading…" : "Refresh"}
            </Button>
          </div>
        }
      >
        {error && <ApiErrorNote error={error} />}
        {data && (
          <div className="space-y-3 text-sm">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge status={data.status} />
              <AuditVerifiedBadge runId={runId} runStatus={data.status} />
              {active && <span className="text-xs text-muted">auto-refreshing…</span>}
              {data.current_step && (
                <span className="text-muted">step: {data.current_step}</span>
              )}
            </div>

            {data.failure_state && (
              <div>
                <div className="mb-1 font-medium text-red-700 dark:text-red-400">Failure</div>
                <Json value={data.failure_state} />
              </div>
            )}

            {data.approval_paused_state && (
              <div>
                <div className="mb-1 flex items-center justify-between gap-3">
                  <span className="font-medium text-amber-700 dark:text-amber-400">
                    Awaiting approval
                  </span>
                  <Link
                    href="/approvals"
                    className="text-xs font-medium text-accent hover:underline"
                  >
                    Resolve in Approvals →
                  </Link>
                </div>
                <Json value={data.approval_paused_state} />
              </div>
            )}

            {data.terminal_output != null && (
              <div>
                <div className="mb-1 font-medium">Output</div>
                <Json value={data.terminal_output} />
              </div>
            )}
          </div>
        )}
      </Card>

      {data && TERMINAL.has(data.status.toLowerCase()) && <QualityVerdictCard runId={runId} />}

      <Card title="Timeline" actions={<Button onClick={loadTimeline}>Load timeline</Button>}>
        {tlError && <ErrorBox message={tlError} />}
        {timeline ? <Json value={timeline} /> : <Empty>Not loaded.</Empty>}
      </Card>
    </div>
  );
}

// Runs the backend accepts a quality verdict for (COMPLETED / FAILED).
const TERMINAL = new Set(["completed", "failed"]);

// Operator controls (RUN_ADMIN): cancel a live run, interrupt a running one,
// replay a failed/dead-letter one. Buttons render for the relevant states; a 403
// (key lacks RUN_ADMIN) surfaces as an inline note rather than hiding silently.
function RunControls({
  runId,
  status,
  onChanged,
}: {
  runId: string;
  status: string;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const s = status.toLowerCase();
  const canCancel = ACTIVE.has(s);
  const canInterrupt = s === "running" || s === "in_progress";
  const canReplay = s === "failed" || s === "dead_letter";

  async function act(label: string, fn: () => Promise<RunStatus>) {
    setBusy(label);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(null);
    }
  }

  if (!canCancel && !canInterrupt && !canReplay) return null;
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        {canInterrupt && (
          <Button
            size="sm"
            onClick={() => act("interrupt", () => interruptRun(runId))}
            disabled={busy !== null}
          >
            {busy === "interrupt" ? "Interrupting…" : "Interrupt"}
          </Button>
        )}
        {canCancel && (
          <Button
            size="sm"
            variant="danger"
            onClick={() => act("cancel", () => cancelRun(runId))}
            disabled={busy !== null}
          >
            {busy === "cancel" ? "Cancelling…" : "Cancel"}
          </Button>
        )}
        {canReplay && (
          <Button
            size="sm"
            variant="primary"
            onClick={() => act("replay", () => replayRun(runId))}
            disabled={busy !== null}
          >
            {busy === "replay" ? "Replaying…" : "Replay"}
          </Button>
        )}
      </div>
      {error && <span className="text-xs text-red-600 dark:text-red-400">{error}</span>}
    </div>
  );
}

// Attach a good/bad quality verdict to a terminal run (METRICS_ADMIN). This is
// the signal that lights up quality-aware unit economics on the Cost page — the
// runtime can't know if an answer was *good*, so a reviewer records it here.
function QualityVerdictCard({ runId }: { runId: string }) {
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function submit(verdict: "good" | "bad") {
    setBusy(verdict);
    setError(null);
    setDone(null);
    try {
      await attachQualityVerdict({
        run_id: runId,
        verdict,
        source: "console",
        detail: detail.trim(),
      });
      setDone(`Recorded “${verdict}”. Quality economics on the Cost page will include this run.`);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="Quality verdict">
      <div className="space-y-3">
        <p className="text-sm text-muted">
          Was this run&apos;s outcome good? Recording a verdict feeds{" "}
          <Link href="/cost" className="font-medium text-accent hover:underline">
            quality-aware unit economics
          </Link>{" "}
          — cost per <em>good</em> outcome, not just per completed run.
        </p>
        <Field label="Detail" hint="optional — why good/bad">
          <Input value={detail} onChange={(e) => setDetail(e.target.value)} />
        </Field>
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            onClick={() => submit("good")}
            disabled={busy !== null}
          >
            {busy === "good" ? "Recording…" : "👍 Good"}
          </Button>
          <Button
            variant="danger"
            onClick={() => submit("bad")}
            disabled={busy !== null}
          >
            {busy === "bad" ? "Recording…" : "👎 Bad"}
          </Button>
        </div>
        {error && <ErrorBox message={error} />}
        {done && (
          <p className="text-sm text-emerald-600 dark:text-emerald-400">{done}</p>
        )}
      </div>
    </Card>
  );
}

// Tamper-evidence marker: verifies the run's audit digest chain. Re-checks on
// status transitions so the badge covers records appended while running.
// Renders nothing when unavailable (e.g. the key lacks audit-read) — absence
// of the feature shouldn't read as a broken chain.
function AuditVerifiedBadge({ runId, runStatus }: { runId: string; runStatus: string }) {
  const [result, setResult] = useState<AuditVerification | null>(null);

  useEffect(() => {
    let cancelled = false;
    getRunAuditVerification(runId)
      .then((r) => {
        if (!cancelled) setResult(r);
      })
      .catch(() => {
        if (!cancelled) setResult(null);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, runStatus]);

  if (result === null || result.record_count === 0) return null;

  const recordLabel = `${result.record_count} record${
    result.record_count === 1 ? "" : "s"
  }`;

  // Digest-continuity axis first: a broken chain is the worst case and shows red
  // regardless of the signature axis.
  if (!result.verified) {
    return (
      <span
        title={`${result.error ?? "Digest chain broken"}${
          result.failed_audit_id ? ` at ${result.failed_audit_id}` : ""
        }`}
        className="inline-flex items-center gap-1.5 rounded-full bg-red-500/12 px-2 py-0.5 text-xs font-medium text-red-700 dark:text-red-400"
      >
        <span aria-hidden>✕</span> Audit chain broken
      </span>
    );
  }

  // Digest chain intact — layer the WS-D keyed-signature axis (three-state).
  // signature_verified === true -> signed & verified (green).
  if (result.signature_verified === true) {
    return (
      <span
        title={`Signed & verified over ${recordLabel}${
          result.signing_key_id ? ` under key ${result.signing_key_id}` : ""
        }`}
        className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/12 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-400"
      >
        <span aria-hidden>✓</span> Signed &amp; verified
      </span>
    );
  }
  // signature_verified === false -> a signed record failed verification (red).
  if (result.signature_verified === false) {
    return (
      <span
        title={`A signed audit record failed signature verification${
          result.failed_audit_id ? ` at ${result.failed_audit_id}` : ""
        }`}
        className="inline-flex items-center gap-1.5 rounded-full bg-red-500/12 px-2 py-0.5 text-xs font-medium text-red-700 dark:text-red-400"
      >
        <span aria-hidden>✕</span> Signature invalid / tampered
      </span>
    );
  }
  // signature_verified == null -> unsigned-legacy: the digest chain is intact but
  // records are not cryptographically signed. Neutral — never green, never red.
  return (
    <span
      title={`Digest chain intact over ${recordLabel}; records are not cryptographically signed (unsigned-legacy)`}
      className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/12 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-400"
    >
      <span aria-hidden>◇</span> Chain intact (unsigned)
    </span>
  );
}
