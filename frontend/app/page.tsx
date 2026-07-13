"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  ApiErrorNote,
  Card,
  Empty,
  fmtTime,
  PageHeader,
  Skeleton,
  StatusBadge,
  useAsync,
} from "@/app/components/ui";
import { getHealth, listDeployments } from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

// Studio first — authoring is the center of the product.
const QUICK_LINKS: { href: string; title: string; desc: string; icon: string }[] = [
  {
    href: "/studio",
    title: "Studio",
    desc: "Author workflows",
    icon: "M12 19l7-7 3 3-7 7-3-3zM18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5zM2 2l7.586 7.586M11 13a2 2 0 11-4 0 2 2 0 014 0z",
  },
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
    href: "/connectors",
    title: "Connectors",
    desc: "Memory & retrieval backends",
    icon: "M12 2C6.5 2 2 3.6 2 5.5v13C2 20.4 6.5 22 12 22s10-1.6 10-3.5v-13C22 3.6 17.5 2 12 2z M2 5.5C2 7.4 6.5 9 12 9s10-1.6 10-3.5 M2 12c0 1.9 4.5 3.5 10 3.5s10-1.6 10-3.5",
  },
  {
    href: "/guide",
    title: "Guide",
    desc: "Concepts & how-to",
    icon: "M4 19.5A2.5 2.5 0 016.5 17H20M4 19.5A2.5 2.5 0 006.5 22H20V2H6.5A2.5 2.5 0 004 4.5v15z",
  },
];

const GETTING_STARTED: { title: string; desc: React.ReactNode; href: string }[] = [
  {
    title: "Connect to your deployment",
    desc: "Top right — set the API base URL and your operator key.",
    href: "/guide",
  },
  {
    title: "Create a workflow from a template",
    desc: "Studio ships ready-made graphs (RAG, approval gate, tool pipeline) you can reshape.",
    href: "/studio",
  },
  {
    title: "Submit a run and watch it execute",
    desc: "Use an example payload, then follow status, output, and timeline.",
    href: "/runs",
  },
  {
    title: "Govern it",
    desc: "Resolve approval gates, inspect the per-node audit trail, track spend.",
    href: "/approvals",
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
                  <div className="text-xs uppercase tracking-wide text-muted">
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

      {connected && <DeploymentsCard />}

      {/* Getting started — most useful before the first workflow exists, so it
          leads while unconnected and stays available (below the fold) after. */}
      <Card title="Getting started">
        <ol className="grid gap-x-8 gap-y-3 sm:grid-cols-2">
          {GETTING_STARTED.map((s, i) => (
            <li key={s.title} className="flex gap-3 text-sm">
              <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-accent/10 text-xs font-semibold text-accent">
                {i + 1}
              </span>
              <div>
                <Link href={s.href} className="font-medium hover:text-accent">
                  {s.title}
                </Link>
                <p className="mt-0.5 text-xs leading-relaxed text-muted">{s.desc}</p>
              </div>
            </li>
          ))}
        </ol>
        <p className="mt-4 border-t border-border pt-3 text-xs text-muted">
          New here?{" "}
          <Link href="/guide" className="font-medium text-accent hover:underline">
            Read the guide
          </Link>{" "}
          — concepts, node reference, and how to call the deployment from your app.
        </p>
      </Card>

      {/* Quick links */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
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

// Every persisted deployment version — including graphs written in code and
// deployed via the medium-code path — with the one this service serves marked.
function DeploymentsCard() {
  const { data, error, loading } = useAsync(listDeployments, []);
  return (
    <Card title="Deployments">
      {error && <ApiErrorNote error={error} />}
      {loading && !data && <Skeleton rows={2} />}
      {data && data.length === 0 && (
        <Empty>No deployments recorded yet — deploy a published graph to see it here.</Empty>
      )}
      {data && data.length > 0 && (
        <ul className="divide-y divide-border">
          {data.map((d) => (
            <li
              key={`${d.deployment_ref}@${d.version}`}
              className="flex items-center justify-between gap-3 py-2.5 first:pt-0 last:pb-0 text-sm"
            >
              <div className="min-w-0">
                <span className="font-medium">{d.deployment_ref}</span>
                <span className="ml-2 text-xs text-muted">v{d.version}</span>
                <div className="truncate font-mono text-xs text-muted">
                  {d.graph_version_ref}
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-3 text-xs text-muted">
                <span>{fmtTime(d.created_at)}</span>
                {d.serving && (
                  <span className="rounded-full bg-accent/10 px-2 py-0.5 font-medium text-accent">
                    Serving
                  </span>
                )}
                <StatusBadge status={d.status} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className={`mt-0.5 ${mono ? "font-mono text-sm" : "text-sm"}`}>{value}</dd>
    </div>
  );
}
