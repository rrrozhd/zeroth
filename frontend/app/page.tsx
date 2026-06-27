"use client";

import Link from "next/link";
import { ConnectBar } from "@/app/components/ConnectBar";
import { Card, ErrorBox, useAsync } from "@/app/components/ui";
import { getHealth } from "@/app/lib/api";

export default function Overview() {
  const { data: health, error, loading, reload } = useAsync(getHealth, []);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Operate and author your Zeroth multi-agent apps.
        </p>
      </header>

      <ConnectBar onChange={reload} />

      <Card title="Connected deployment">
        {loading && <p className="text-sm text-zinc-500">Checking…</p>}
        {error && <ErrorBox message={error} />}
        {health && (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
            <Field label="Status" value={health.status} />
            <Field label="Deployment" value={health.deployment_ref} />
            <Field label="Version" value={`v${health.deployment_version}`} />
            <Field label="Graph" value={health.graph_version_ref} />
          </dl>
        )}
      </Card>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {[
          ["/runs", "Runs", "Submit & inspect runs"],
          ["/approvals", "Approvals", "Resolve human gates"],
          ["/audit", "Audit", "Per-node audit trail"],
          ["/cost", "Cost", "Spend for this deployment"],
          ["/studio", "Studio", "Author workflows"],
        ].map(([href, title, desc]) => (
          <Link
            key={href}
            href={href}
            className="rounded-lg border border-zinc-200 bg-white p-4 transition-colors hover:border-zinc-300 hover:bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950 dark:hover:bg-zinc-900"
          >
            <div className="font-medium">{title}</div>
            <div className="mt-0.5 text-xs text-zinc-500">{desc}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-zinc-400">{label}</dt>
      <dd className="font-mono text-sm">{value}</dd>
    </div>
  );
}
