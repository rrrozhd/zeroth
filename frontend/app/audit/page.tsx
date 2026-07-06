"use client";

import {
  ApiErrorNote,
  Button,
  Empty,
  fmtTime,
  fmtUsd,
  Json,
  NotConnected,
  PageHeader,
  Skeleton,
  StatusBadge,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import { listAudits, type NodeAuditRecord } from "@/app/lib/api";

export default function AuditPage() {
  const connected = useConnected();
  const { data, error, loading, reload } = useAsync(listAudits, []);
  const records = data?.records ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit"
        subtitle="Per-node audit records for this deployment."
        actions={
          <Button onClick={() => reload()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      />

      {!connected && <NotConnected />}
      {connected && error && <ApiErrorNote error={error} />}
      {connected && loading && !data && <Skeleton rows={5} />}
      {connected && data && records.length === 0 && <Empty>No audit records yet.</Empty>}

      <div className="space-y-2">
        {records.map((r) => (
          <AuditRow key={r.audit_id} record={r} />
        ))}
      </div>
    </div>
  );
}

function AuditRow({ record }: { record: NodeAuditRecord }) {
  return (
    <details className="group rounded-xl border border-border bg-surface">
      <summary className="flex cursor-pointer items-center justify-between px-4 py-3 [&::-webkit-details-marker]:hidden">
        <span className="flex min-w-0 items-center gap-3">
          <svg
            aria-hidden
            viewBox="0 0 16 16"
            fill="none"
            className="h-3.5 w-3.5 shrink-0 text-muted transition-transform group-open:rotate-90"
          >
            <path
              d="M6 4l4 4-4 4"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="font-medium">{record.node_id}</span>
          <span className="truncate font-mono text-xs text-muted">{record.run_id}</span>
        </span>
        <span className="flex shrink-0 items-center gap-3 text-xs text-muted">
          {record.started_at && <span>{fmtTime(record.started_at)}</span>}
          {record.cost_usd != null && <span>{fmtUsd(record.cost_usd)}</span>}
          {record.attempt != null && <span>attempt {record.attempt}</span>}
          <StatusBadge status={record.status} />
        </span>
      </summary>
      <div className="space-y-3 border-t border-border p-4 text-sm">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
          <Field label="Audit ID" value={record.audit_id} mono />
          <Field label="Started" value={fmtTime(record.started_at)} />
          <Field label="Completed" value={fmtTime(record.completed_at)} />
          {record.token_usage && (
            <Field
              label="Tokens"
              value={`${record.token_usage.input_tokens ?? 0} in / ${record.token_usage.output_tokens ?? 0} out`}
            />
          )}
        </dl>
        {record.error && (
          <div className="text-red-700 dark:text-red-400">{record.error}</div>
        )}
        {(record.tool_calls?.length ?? 0) > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-muted">
              Tool calls ({record.tool_calls!.length})
            </summary>
            <div className="mt-2">
              <Json value={record.tool_calls} />
            </div>
          </details>
        )}
        {(record.memory_interactions?.length ?? 0) > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-muted">
              Memory interactions ({record.memory_interactions!.length})
            </summary>
            <div className="mt-2">
              <Json value={record.memory_interactions} />
            </div>
          </details>
        )}
        {record.output_snapshot != null && (
          <details>
            <summary className="cursor-pointer text-xs text-muted">Output snapshot</summary>
            <div className="mt-2">
              <Json value={record.output_snapshot} />
            </div>
          </details>
        )}
      </div>
    </details>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className={mono ? "font-mono" : ""}>{value}</dd>
    </div>
  );
}
