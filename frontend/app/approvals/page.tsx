"use client";

import { useState } from "react";
import {
  ApiErrorNote,
  Button,
  Card,
  Empty,
  ErrorBox,
  Json,
  NotConnected,
  PageHeader,
  StatusBadge,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import { errMsg, listApprovals, resolveApproval, type ApprovalRecord } from "@/app/lib/api";

export default function ApprovalsPage() {
  const connected = useConnected();
  const { data, error, loading, reload } = useAsync(listApprovals, []);
  const pending = (data ?? []).filter((a) => (a.status ?? "pending") === "pending");
  const resolved = (data ?? []).filter((a) => (a.status ?? "pending") !== "pending");

  return (
    <div className="space-y-6">
      <PageHeader
        title="Approvals"
        subtitle="Resolve human-approval gates."
        actions={
          <Button onClick={() => reload()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      />

      {!connected && <NotConnected />}
      {connected && error && <ApiErrorNote error={error} />}
      {connected && data && pending.length === 0 && <Empty>No pending approvals.</Empty>}

      <div className="space-y-3">
        {pending.map((a) => (
          <ApprovalCard key={a.approval_id} approval={a} onResolved={reload} />
        ))}
      </div>

      {resolved.length > 0 && (
        <Card title="Resolved">
          <ul className="divide-y divide-border">
            {resolved.map((a) => (
              <li
                key={a.approval_id}
                className="flex items-center justify-between py-2 text-sm"
              >
                <span>{a.summary}</span>
                <StatusBadge status={a.status ?? "resolved"} />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function ApprovalCard({
  approval,
  onResolved,
}: {
  approval: ApprovalRecord;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function resolve(decision: "approve" | "reject") {
    if (!approval.approval_id) return;
    setBusy(decision);
    setError(null);
    try {
      await resolveApproval(approval.approval_id, { decision });
      onResolved();
    } catch (e) {
      setError(errMsg(e));
      setBusy(null);
    }
  }

  return (
    <Card
      title={approval.summary}
      actions={<StatusBadge status={approval.status ?? "pending"} />}
    >
      <div className="space-y-3 text-sm">
        <p className="text-zinc-600 dark:text-zinc-400">{approval.rationale}</p>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-zinc-500 sm:grid-cols-3">
          <div>
            <dt className="text-zinc-400">Node</dt>
            <dd className="font-mono">{approval.node_id}</dd>
          </div>
          <div>
            <dt className="text-zinc-400">Run</dt>
            <dd className="font-mono">{approval.run_id}</dd>
          </div>
          {approval.sla_deadline && (
            <div>
              <dt className="text-zinc-400">SLA</dt>
              <dd>{approval.sla_deadline}</dd>
            </div>
          )}
        </dl>

        {approval.proposed_payload != null && (
          <details>
            <summary className="cursor-pointer text-xs text-zinc-500">
              Proposed payload
            </summary>
            <div className="mt-2">
              <Json value={approval.proposed_payload} />
            </div>
          </details>
        )}

        {error && <ErrorBox message={error} />}

        <div className="flex gap-2">
          <Button variant="primary" onClick={() => resolve("approve")} disabled={!!busy}>
            {busy === "approve" ? "Approving…" : "Approve"}
          </Button>
          <Button variant="danger" onClick={() => resolve("reject")} disabled={!!busy}>
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </Button>
        </div>
      </div>
    </Card>
  );
}
