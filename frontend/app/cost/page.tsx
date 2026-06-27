"use client";

import { Button, Card, ErrorBox, useAsync } from "@/app/components/ui";
import { getCost } from "@/app/lib/api";

export default function CostPage() {
  const { data, error, loading, reload } = useAsync(getCost, []);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Cost</h1>
        <Button onClick={reload} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </Button>
      </div>

      {error && <ErrorBox message={error} />}

      {data && (
        <Card title={`Deployment: ${data.deployment_ref}`}>
          <div className="flex items-baseline gap-2">
            <span className="text-4xl font-semibold tabular-nums">
              {(data.currency === "USD" || !data.currency ? "$" : "")}
              {data.total_cost_usd.toFixed(4)}
            </span>
            <span className="text-sm text-zinc-500">{data.currency ?? "USD"} total</span>
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            Aggregated provider cost attributed to this deployment.
          </p>
        </Card>
      )}
    </div>
  );
}
