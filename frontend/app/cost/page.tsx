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
import {
  getCost,
  getRightsizingOpportunities,
  getUnitEconomics,
  getWaste,
} from "@/app/lib/api";
import type {
  NodeSpend,
  QualityEconomics,
  SpendReport,
  TenantEconomics,
  UnitEconomicsReport,
  WasteRollup,
  WasteRollupFinding,
  WorkflowEconomics,
} from "@/app/lib/api";

export default function CostPage() {
  const connected = useConnected();
  const { data, error, loading, reload } = useAsync(getCost, []);
  const econ = useAsync(getUnitEconomics, []);
  const waste = useAsync(getWaste, []);
  const opp = useAsync(getRightsizingOpportunities, []);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cost"
        subtitle="Aggregated provider spend."
        actions={
          <Button
            onClick={() => {
              reload();
              econ.reload();
              waste.reload();
              opp.reload();
            }}
            disabled={loading}
          >
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

      {/* Older backends without the endpoint report an error here — stay quiet. */}
      {connected && econ.data && !econ.error && <UnitEconomics report={econ.data} />}
      {connected && waste.data && !waste.error && <EconomicWaste report={waste.data} />}
      {connected && opp.data && !opp.error && <RightsizingOpportunities report={opp.data} />}
    </div>
  );
}

function pct(n: number): string {
  return `${Math.round(n * 100)}%`;
}

function Stat({
  label,
  value,
  emphasis,
  tone,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
  tone?: "warn";
}) {
  return (
    <div>
      <div
        className={`tabular-nums ${emphasis ? "text-3xl font-semibold" : "text-lg font-medium"} ${
          tone === "warn" ? "text-amber-700 dark:text-amber-400" : ""
        }`}
      >
        {value}
      </div>
      <div className="text-xs text-muted">{label}</div>
    </div>
  );
}

function UnitEconomics({ report }: { report: UnitEconomicsReport }) {
  const headline = report.cost_per_successful_run_usd;
  const noData = report.window_runs === 0 || report.runs_with_cost === 0 || headline == null;

  return (
    <Card title="Unit economics">
      <p className="mb-3 text-xs text-muted">
        What one successful outcome actually costs — spend on terminal (finished) runs divided by
        the ones that succeeded, so the price of failures is loaded onto each good result.
      </p>

      {noData ? (
        <p className="text-sm text-muted">{report.note}</p>
      ) : (
        <>
          <p className="mb-3 text-xs text-muted">Over the last {report.window_runs} runs.</p>
          <div className="flex flex-wrap gap-x-8 gap-y-3">
            <Stat label="Cost / successful run" value={fmtUsd(headline)} emphasis />
            <Stat label="Success rate" value={pct(report.success_rate)} />
            <Stat
              label="Failure tax"
              value={`${fmtUsd(report.failure_tax_usd)} · ${pct(report.failure_tax_ratio)}`}
              tone={report.failure_tax_usd > 0 ? "warn" : undefined}
            />
            <Stat
              label="Runs (ok / failed / in-flight)"
              value={`${report.successful_runs} / ${report.failed_runs} / ${report.in_flight_runs}`}
            />
          </div>

          {report.failure_tax_usd > 0 && report.mean_cost_per_successful_run_usd != null && (
            <p className="mt-2 text-xs text-muted">
              Without the failure tax a success would cost{" "}
              {fmtUsd(report.mean_cost_per_successful_run_usd)}.
            </p>
          )}

          {report.by_workflow.length > 1 && (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs text-muted">
                  <tr className="border-b border-border text-left">
                    <th className="py-1.5 pr-3 font-medium">Workflow</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Success</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Cost / success</th>
                    <th className="py-1.5 text-right font-medium">Failure tax</th>
                  </tr>
                </thead>
                <tbody>
                  {report.by_workflow.map((w: WorkflowEconomics) => (
                    <tr key={w.workflow_name} className="border-b border-border/60 last:border-0">
                      <td className="py-1.5 pr-3 font-mono text-xs">{w.workflow_name}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {pct(w.success_rate)}
                      </td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {w.cost_per_successful_run_usd != null
                          ? fmtUsd(w.cost_per_successful_run_usd)
                          : "—"}
                      </td>
                      <td className="py-1.5 text-right tabular-nums">{fmtUsd(w.failure_tax_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {(report.by_tenant?.length ?? 0) > 1 && (
            <div className="mt-4 overflow-x-auto">
              <p className="mb-1 text-xs text-muted">By tenant — which customer is unprofitable</p>
              <table className="w-full text-sm">
                <thead className="text-xs text-muted">
                  <tr className="border-b border-border text-left">
                    <th className="py-1.5 pr-3 font-medium">Tenant</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Success</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Cost / success</th>
                    <th className="py-1.5 text-right font-medium">Failure tax</th>
                  </tr>
                </thead>
                <tbody>
                  {report.by_tenant.map((t: TenantEconomics) => (
                    <tr key={t.tenant_id} className="border-b border-border/60 last:border-0">
                      <td className="py-1.5 pr-3 font-mono text-xs">{t.tenant_id}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{pct(t.success_rate)}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">
                        {t.cost_per_successful_run_usd != null
                          ? fmtUsd(t.cost_per_successful_run_usd)
                          : "—"}
                      </td>
                      <td className="py-1.5 text-right tabular-nums">{fmtUsd(t.failure_tax_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {report.quality && <QualityOverlay quality={report.quality} />}

          <p className="mt-3 text-xs text-muted">{report.note}</p>
        </>
      )}
    </Card>
  );
}

function QualityOverlay({ quality }: { quality: QualityEconomics }) {
  if (quality.state === "ok" && quality.cost_per_quality_success_usd != null) {
    return (
      <div className="mt-4 rounded-xl border border-border p-3">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-2xl font-semibold tabular-nums">
            {fmtUsd(quality.cost_per_quality_success_usd)}
          </span>
          <span className="text-xs text-muted">
            cost / good outcome · {pct(quality.coverage)} labeled · via {quality.sources.join(", ")}
          </span>
        </div>
        <p className="mt-1 text-xs text-muted">
          A stricter success — only runs a reviewer judged good.{" "}
          {pct(quality.quality_success_rate_over_labeled)} of labeled runs were good.
        </p>
      </div>
    );
  }
  return <p className="mt-3 text-xs text-muted">Quality: {quality.note}</p>;
}

function fmtUsd(n: number): string {
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

function RightsizingOpportunities({ report }: { report: SpendReport }) {
  return (
    <Card title="Right-sizing opportunities">
      <p className="mb-3 text-xs text-muted">
        Agent nodes ranked by spend. A cheaper model that can still do the job is a candidate
        to test — open the node in Studio and run &ldquo;Measure equivalence&rdquo;.
      </p>

      {report.nodes.length === 0 ? (
        <p className="text-sm text-muted">{report.note}</p>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted">
                <tr className="border-b border-border text-left">
                  <th className="py-1.5 pr-3 font-medium">Node</th>
                  <th className="py-1.5 pr-3 font-medium">Model</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Runs</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Spend</th>
                  <th className="py-1.5 font-medium">Opportunity</th>
                </tr>
              </thead>
              <tbody>
                {report.nodes.map((n) => (
                  <OpportunityRow key={n.node_id} node={n} />
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-3 text-xs text-muted">{report.note}</p>
        </>
      )}
    </Card>
  );
}

function OpportunityRow({ node }: { node: NodeSpend }) {
  return (
    <tr className="border-b border-border/60 last:border-0">
      <td className="py-1.5 pr-3 font-mono text-xs">{node.node_id}</td>
      <td className="py-1.5 pr-3 font-mono text-xs">{node.incumbent_model ?? "—"}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums">{node.runs}</td>
      <td className="py-1.5 pr-3 text-right tabular-nums">{fmtUsd(node.total_cost_usd)}</td>
      <td className="py-1.5">
        {node.best_savings_pct != null ? (
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-400">
              up to −{Math.round(node.best_savings_pct)}%
            </span>
            {node.projected_savings_usd != null && (
              <span className="text-xs text-muted">≈ {fmtUsd(node.projected_savings_usd)}</span>
            )}
            {node.experiment_ready ? (
              <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                testable
              </span>
            ) : node.uses_tools ? (
              <span
                className="text-[10px] text-muted"
                title="Tool-using agent — faithful replay not yet supported"
              >
                tools — not yet testable
              </span>
            ) : null}
          </span>
        ) : (
          <span className="text-xs text-muted">no cheaper capable model</span>
        )}
      </td>
    </tr>
  );
}

function EconomicWaste({ report }: { report: WasteRollup }) {
  const totalWaste = report.total_confirmed_waste_usd + report.total_flagged_waste_usd;
  const noData = report.window_runs === 0 || report.runs_with_cost === 0 || totalWaste === 0;

  return (
    <Card title="Economic waste">
      <p className="mb-3 text-xs text-muted">
        Spend attributed to structural causes — runs that paid but failed (confirmed waste), and
        loops/retries that may be recoverable (flagged). The two never share a dollar.
      </p>

      {noData ? (
        <p className="text-sm text-muted">{report.note}</p>
      ) : (
        <>
          <div className="flex flex-wrap gap-x-8 gap-y-3">
            <Stat
              label="Confirmed waste"
              value={fmtUsd(report.total_confirmed_waste_usd)}
              emphasis
              tone={report.total_confirmed_waste_usd > 0 ? "warn" : undefined}
            />
            <Stat label="Flagged waste" value={fmtUsd(report.total_flagged_waste_usd)} />
            <Stat label="Of spend" value={pct(report.waste_ratio)} />
            <Stat label="Runs with waste" value={`${report.runs_with_waste} / ${report.window_runs}`} />
          </div>

          {report.top_findings.length > 0 && (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-xs text-muted">
                  <tr className="border-b border-border text-left">
                    <th className="py-1.5 pr-3 font-medium">Kind</th>
                    <th className="py-1.5 pr-3 font-medium">Where</th>
                    <th className="py-1.5 pr-3 text-right font-medium">Wasted</th>
                    <th className="py-1.5 font-medium">Type</th>
                  </tr>
                </thead>
                <tbody>
                  {report.top_findings.map((f: WasteRollupFinding, i: number) => (
                    <tr
                      key={`${f.run_id}-${f.kind}-${i}`}
                      className="border-b border-border/60 last:border-0"
                    >
                      <td className="py-1.5 pr-3 font-mono text-xs">{f.kind}</td>
                      <td className="py-1.5 pr-3 font-mono text-xs">{f.node_id ?? f.run_id}</td>
                      <td className="py-1.5 pr-3 text-right tabular-nums">{fmtUsd(f.wasted_usd)}</td>
                      <td className="py-1.5">
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                            f.confirmed
                              ? "bg-amber-500/15 text-amber-700 dark:text-amber-400"
                              : "bg-border/60 text-muted"
                          }`}
                        >
                          {f.confirmed ? "confirmed" : "flagged"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="mt-3 text-xs text-muted">{report.note}</p>
        </>
      )}
    </Card>
  );
}
