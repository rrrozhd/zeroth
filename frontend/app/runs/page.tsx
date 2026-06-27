"use client";

import { useEffect, useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Field,
  Input,
  Json,
  Mono,
  NotConnected,
  PageHeader,
  StatusBadge,
  Textarea,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import {
  errMsg,
  getRun,
  getRunTimeline,
  listRuns,
  submitRun,
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

  return (
    <div className="space-y-6">
      <PageHeader title="Runs" subtitle="Submit and inspect runs." />
      {!connected ? (
        <NotConnected />
      ) : selected ? (
        <RunDetail runId={selected} onBack={() => setSelected(null)} />
      ) : (
        <>
          <SubmitRun onSubmitted={setSelected} />
          <RunList onSelect={setSelected} />
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
      {data && data.runs.length === 0 && <Empty>No runs yet.</Empty>}
      {data && data.runs.length > 0 && (
        <ul className="divide-y divide-border">
          {data.runs.map((r) => (
            <li key={r.run_id}>
              <button
                onClick={() => onSelect(r.run_id)}
                className="flex w-full items-center justify-between gap-3 py-2.5 text-left hover:opacity-80"
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

function SubmitRun({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const [payload, setPayload] = useState('{\n  "question": "What is Zeroth?"\n}');
  const [thread, setThread] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        <Field label="Input payload (JSON)">
          <Textarea
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
            rows={5}
            className="font-mono text-xs"
          />
        </Field>
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
          <Button onClick={() => reload()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      >
        {error && <ApiErrorNote error={error} />}
        {data && (
          <div className="space-y-3 text-sm">
            <div className="flex items-center gap-3">
              <StatusBadge status={data.status} />
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
                <div className="mb-1 font-medium text-amber-700 dark:text-amber-400">
                  Awaiting approval
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

      <Card title="Timeline" actions={<Button onClick={loadTimeline}>Load timeline</Button>}>
        {tlError && <ErrorBox message={tlError} />}
        {timeline ? <Json value={timeline} /> : <Empty>Not loaded.</Empty>}
      </Card>
    </div>
  );
}
