"use client";

import { useEffect, useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Json,
  Mono,
  PageHeader,
  StatusBadge,
  useAsync,
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
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    setSelected(new URLSearchParams(window.location.search).get("run_id"));
  }, []);

  return (
    <div className="space-y-6">
      <PageHeader title="Runs" subtitle="Submit and inspect runs." />
      {selected ? (
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
        <Button onClick={reload} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      }
    >
      {error && <ApiErrorNote error={error} />}
      {data && data.runs.length === 0 && <Empty>No runs yet.</Empty>}
      {data && data.runs.length > 0 && (
        <ul className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {data.runs.map((r) => (
            <li key={r.run_id}>
              <button
                onClick={() => onSelect(r.run_id)}
                className="flex w-full items-center justify-between py-2.5 text-left hover:opacity-80"
              >
                <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">
                  {r.run_id}
                </span>
                <span className="flex items-center gap-3 text-xs text-zinc-500">
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
      setError("input_payload is not valid JSON");
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
        <label className="block text-sm">
          <span className="mb-1 block font-medium text-zinc-700 dark:text-zinc-300">
            input_payload (JSON)
          </span>
          <textarea
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
            rows={5}
            className="w-full rounded-md border border-zinc-300 p-2 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-900"
          />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-medium text-zinc-700 dark:text-zinc-300">
            thread_id <span className="font-normal text-zinc-400">(optional)</span>
          </span>
          <input
            value={thread}
            onChange={(e) => setThread(e.target.value)}
            className="w-full rounded-md border border-zinc-300 px-3 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-900"
          />
        </label>
        <Button variant="primary" onClick={submit} disabled={busy}>
          {busy ? "Submitting…" : "Submit run"}
        </Button>
      </div>
    </Card>
  );
}

function RunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const { data, error, loading, reload } = useAsync<RunStatus>(
    () => getRun(runId),
    [runId],
  );
  const [timeline, setTimeline] = useState<unknown>(null);
  const [tlError, setTlError] = useState<string | null>(null);

  // Poll while the run is still active.
  const active = data ? ACTIVE.has(data.status.toLowerCase()) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(reload, 1500);
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
      <button onClick={onBack} className="text-sm text-zinc-500 hover:underline">
        ← Back to runs
      </button>

      <Card
        title={<Mono>{runId}</Mono>}
        actions={
          <Button onClick={reload} disabled={loading}>
            {loading ? "…" : "Refresh"}
          </Button>
        }
      >
        {error && <ErrorBox message={error} />}
        {data && (
          <div className="space-y-3 text-sm">
            <div className="flex items-center gap-3">
              <StatusBadge status={data.status} />
              {active && <span className="text-xs text-zinc-400">auto-refreshing…</span>}
              {data.current_step && (
                <span className="text-zinc-500">step: {data.current_step}</span>
              )}
            </div>

            {data.failure_state && (
              <div>
                <div className="mb-1 font-medium text-red-700 dark:text-red-400">
                  Failure
                </div>
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

      <Card
        title="Timeline"
        actions={<Button onClick={loadTimeline}>Load timeline</Button>}
      >
        {tlError && <ErrorBox message={tlError} />}
        {timeline ? <Json value={timeline} /> : <Empty>Not loaded.</Empty>}
      </Card>
    </div>
  );
}
