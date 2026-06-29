"use client";

import {
  ApiErrorNote,
  Button,
  Empty,
  Json,
  NotConnected,
  PageHeader,
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
    <details className="rounded-xl border border-border bg-surface">
      <summary className="flex cursor-pointer items-center justify-between px-4 py-3">
        <span className="flex items-center gap-3">
          <span className="font-medium">{record.node_id}</span>
          <span className="font-mono text-xs text-muted">{record.run_id}</span>
        </span>
        <span className="flex items-center gap-3 text-xs text-muted">
          {record.cost_usd != null && <span>${record.cost_usd.toFixed(4)}</span>}
          {record.attempt != null && <span>attempt {record.attempt}</span>}
          <StatusBadge status={record.status} />
        </span>
      </summary>
      <div className="space-y-3 border-t border-border p-4 text-sm">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-muted sm:grid-cols-3">
          <Field label="Audit ID" value={record.audit_id} mono />
          <Field label="Started" value={record.started_at ?? "—"} />
          <Field label="Completed" value={record.completed_at ?? "—"} />
        </dl>
        {record.error && (
          <div className="text-red-700 dark:text-red-400">{record.error}</div>
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

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className={mono ? "font-mono" : ""}>{value}</dd>
    </div>
  );
}
