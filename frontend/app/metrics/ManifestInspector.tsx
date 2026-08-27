"use client";

import Link from "next/link";

import { Card, CodeBlock, Pill, Skeleton } from "@/app/components/primitives";
import { useLoad } from "@/app/hooks/useLoad";
import {
  getManifest,
  listManifestRuns,
  type ManifestDetail,
} from "@/app/lib/api";

function text(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}

function json(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

const GRID_FIELDS: Array<[keyof ManifestDetail, string]> = [
  ["version", "version"],
  ["onboarding_mode", "onboarding"],
  ["artifact_source_kind", "artifact type"],
  ["entrypoint_type", "entrypoint"],
  ["input_mode", "input mode"],
  ["output_mode", "output mode"],
  ["timeout_seconds", "timeout (seconds)"],
  ["execution_placement", "placement"],
  ["side_effect", "side effect"],
];

export function ManifestInspector({ manifestRef }: { manifestRef: string }) {
  const detail = useLoad(() => getManifest(manifestRef));
  const runLinks = useLoad(() => listManifestRuns(manifestRef));
  const runs = runLinks.data?.runs ?? [];

  return (
    <div data-evidence-scope={`manifest-${manifestRef}`} style={{ padding: "0 16px 16px" }}>
      <Card pad={16} style={{ background: "var(--bg-raised)" }}>
        {detail.loading && !detail.data ? (
          <div style={{ display: "grid", gap: 10 }}>
            <Skeleton height={54} />
            <Skeleton height={110} />
          </div>
        ) : detail.error ? (
          <p role="alert" style={{ margin: 0, color: "var(--danger)", fontSize: 12.5 }}>
            Manifest detail unavailable: {detail.error}
          </p>
        ) : detail.data ? (
          <>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="font-mono text-xs font-semibold text-foreground">
                  {detail.data.manifest_ref}
                </div>
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  Commands, source, environment, and secret bindings stay hidden from this view.
                </p>
              </div>
              <Pill tone={detail.data.side_effect ? "warning" : "success"}>
                {detail.data.side_effect ? "side effects" : "read-only"}
              </Pill>
            </div>

            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(136px, 1fr))",
                gap: 10,
                margin: "14px 0 0",
              }}
            >
              {GRID_FIELDS.map(([key, label]) => (
                <div key={key} style={{ minWidth: 0 }}>
                  <dt className="font-mono text-[10px] uppercase tracking-[0.08em] text-faint">
                    {label}
                  </dt>
                  <dd className="mt-1 truncate font-mono text-xs text-foreground">
                    {text(detail.data?.[key])}
                  </dd>
                </div>
              ))}
            </dl>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
                gap: 12,
                marginTop: 14,
              }}
            >
              <Definition label="content hash" value={detail.data.content_hash} />
              <Definition label="input contract" value={detail.data.input_contract_ref} />
              <Definition label="output contract" value={detail.data.output_contract_ref} />
              <Definition
                label="capability requests"
                value={detail.data.capability_requests?.join(", ") || "—"}
              />
            </div>

            <div className="mt-4 grid gap-3 lg:grid-cols-2">
              <div data-evidence-scope="input-schema">
                <CodeBlock label="Input schema" code={json(detail.data.input_schema)} />
              </div>
              <div data-evidence-scope="output-schema">
                <CodeBlock label="Output schema" code={json(detail.data.output_schema)} />
              </div>
            </div>
            <div className="mt-3" data-evidence-scope="resource-limits">
              <CodeBlock label="Resource limits" code={json(detail.data.resource_limits)} />
            </div>
          </>
        ) : null}

        <div style={{ marginTop: detail.data ? 16 : 0 }}>
          <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-faint">
            Recent linked runs
          </div>
          {runLinks.loading && !runLinks.data ? (
            <div className="mt-2"><Skeleton height={30} /></div>
          ) : runLinks.error ? (
            <p className="mt-2 text-xs leading-relaxed text-muted">
              Run linkage is hidden because this API key cannot read audit evidence for this
              tenant and workspace.
            </p>
          ) : runs.length === 0 ? (
            <p className="mt-2 text-xs text-muted">No retained audit records link this manifest to a run.</p>
          ) : (
            <div className="mt-2 flex flex-wrap gap-2">
              {runs.map((run) => (
                <Link
                  key={`${run.run_id}:${run.node_id}`}
                  href={`/runs/?run=${encodeURIComponent(run.run_id)}`}
                  className="rounded-md border border-border bg-surface px-2.5 py-1.5 font-mono text-xs text-foreground hover:bg-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  {run.run_id} · {run.node_id} · {run.status}
                </Link>
              ))}
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function Definition({ label, value }: { label: string; value: unknown }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-faint">{label}</div>
      <div title={text(value)} className="mt-1 truncate font-mono text-xs text-foreground">
        {text(value)}
      </div>
    </div>
  );
}
