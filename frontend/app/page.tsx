"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Card, PageHeader, StatusBadge, useAsync } from "@/app/components/ui";
import { getHealth } from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

const QUICK_LINKS: { href: string; title: string; desc: string; icon: string }[] = [
  { href: "/runs", title: "Runs", desc: "Submit & inspect runs", icon: "M5 3l14 9-14 9V3z" },
  {
    href: "/approvals",
    title: "Approvals",
    desc: "Resolve human gates",
    icon: "M20 6L9 17l-5-5",
  },
  {
    href: "/audit",
    title: "Audit",
    desc: "Per-node audit trail",
    icon: "M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11",
  },
  {
    href: "/cost",
    title: "Cost",
    desc: "Spend for this deployment",
    icon: "M12 1v22M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
  },
  {
    href: "/studio",
    title: "Studio",
    desc: "Author workflows",
    icon: "M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.586 7.586M11 13a2 2 0 11-4 0 2 2 0 014 0z",
  },
];

export default function Overview() {
  const { data: health, error, loading } = useAsync(getHealth, []);
  // Gate localStorage-derived rendering until mounted to avoid hydration mismatch.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  return (
    <div className="space-y-6">
      <PageHeader title="Overview" subtitle="Operate and author your Zeroth multi-agent apps." />

      {/* Deployment hero */}
      <Card>
        {!mounted || loading ? (
          <p className="text-sm text-muted">Checking deployment…</p>
        ) : !connected ? (
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="font-medium">Not connected</div>
              <p className="mt-0.5 text-sm text-muted">
                Use <span className="font-medium text-foreground">Connect</span> in the top
                right to set your API base URL and key.
              </p>
            </div>
          </div>
        ) : error ? (
          <p className="text-sm text-red-600 dark:text-red-400">{error}</p>
        ) : (
          health && (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs uppercase tracking-wide text-zinc-400">
                    Connected deployment
                  </div>
                  <div className="mt-0.5 text-lg font-semibold">{health.deployment_ref}</div>
                </div>
                <StatusBadge status={health.status} />
              </div>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-border pt-4 text-sm sm:grid-cols-3">
                <Field label="Version" value={`v${health.deployment_version}`} />
                <Field label="Graph" value={health.graph_version_ref} mono />
              </dl>
            </div>
          )
        )}
      </Card>

      {/* Quick links */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
          Jump to
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {QUICK_LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className="group flex items-start gap-3 rounded-xl border border-border bg-surface p-4 shadow-sm shadow-black/[0.03] transition-colors hover:border-accent/40 hover:bg-accent/[0.03]"
            >
              <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent">
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="h-4 w-4"
                >
                  <path d={l.icon} />
                </svg>
              </span>
              <span>
                <span className="block font-medium">{l.title}</span>
                <span className="mt-0.5 block text-xs text-muted">{l.desc}</span>
              </span>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-zinc-400">{label}</dt>
      <dd className={`mt-0.5 ${mono ? "font-mono text-sm" : "text-sm"}`}>{value}</dd>
    </div>
  );
}
