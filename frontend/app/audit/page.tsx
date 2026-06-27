"use client";

import { ApiErrorNote, Button, Empty, Json, StatusBadge, useAsync } from "@/app/components/ui";
import { listAudits, type NodeAuditRecord } from "@/app/lib/api";

export default function AuditPage() {
  const { data, error, loading, reload } = useAsync(listAudits, []);
  const records = data?.records ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Audit</h1>
        <Button onClick={reload} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      </div>

      {error && <ApiErrorNote error={error} />}
      {data && records.length === 0 && <Empty>No audit records yet.</Empty>}

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
    <details className="rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      <summary className="flex cursor-pointer items-center justify-between px-4 py-3">
        <span className="flex items-center gap-3">
          <span className="font-medium">{record.node_id}</span>
          <span className="font-mono text-xs text-zinc-500">{record.run_id}</span>
        </span>
        <span className="flex items-center gap-3 text-xs text-zinc-500">
          {record.cost_usd != null && <span>${record.cost_usd.toFixed(4)}</span>}
          {record.attempt != null && <span>attempt {record.attempt}</span>}
          <StatusBadge status={record.status} />
        </span>
      </summary>
      <div className="space-y-3 border-t border-zinc-100 p-4 text-sm dark:border-zinc-800">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-zinc-500 sm:grid-cols-3">
          <Field label="Audit ID" value={record.audit_id} mono />
          <Field label="Started" value={record.started_at ?? "—"} />
          <Field label="Completed" value={record.completed_at ?? "—"} />
        </dl>
        {record.error && (
          <div className="text-red-700 dark:text-red-400">{record.error}</div>
        )}
        {record.output_snapshot != null && (
          <details>
            <summary className="cursor-pointer text-xs text-zinc-500">Output snapshot</summary>
            <div className="mt-2">
              <Json value={record.output_snapshot} />
            </div>
          </details>
        )}
      </div>
    </details>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-zinc-400">{label}</dt>
      <dd className={mono ? "font-mono" : ""}>{value}</dd>
    </div>
  );
}
