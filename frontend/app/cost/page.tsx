"use client";

import {
  ApiErrorNote,
  Button,
  Card,
  NotConnected,
  PageHeader,
  useAsync,
  useConnected,
} from "@/app/components/ui";
import { getCost } from "@/app/lib/api";

export default function CostPage() {
  const connected = useConnected();
  const { data, error, loading, reload } = useAsync(getCost, []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cost"
        subtitle="Aggregated provider spend."
        actions={
          <Button onClick={() => reload()} disabled={loading}>
            {loading ? "Loading…" : "Refresh"}
          </Button>
        }
      />

      {!connected && <NotConnected />}
      {connected && error && <ApiErrorNote error={error} />}

      {connected && data && (
        <Card title={`Deployment: ${data.deployment_ref}`}>
          <div className="flex items-baseline gap-2">
            <span className="text-4xl font-semibold tabular-nums">
              {data.currency === "USD" || !data.currency ? "$" : ""}
              {data.total_cost_usd >= 1
                ? data.total_cost_usd.toFixed(2)
                : data.total_cost_usd.toFixed(4)}
            </span>
            <span className="text-sm text-muted">{data.currency ?? "USD"} total</span>
          </div>
          <p className="mt-2 text-xs text-muted">
            Aggregated provider cost attributed to this deployment.
          </p>
        </Card>
      )}
    </div>
  );
}
