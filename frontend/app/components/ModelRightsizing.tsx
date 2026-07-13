"use client";

import { useState } from "react";
import { Button } from "@/app/components/ui";
import { errMsg, getRightsizing, runRightsizingExperiment } from "@/app/lib/api";
import type {
  ExperimentReport,
  RightsizingOption,
  RightsizingResult,
} from "@/app/lib/api";

// USD per 1M tokens — keep small values legible ($0.15, $0.075) instead of
// rounding sub-cent prices to $0.00.
function perMtok(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  return `$${usd.toPrecision(2)}`;
}

function candidateRef(c: RightsizingOption): string {
  return c.provider ? `${c.provider}/${c.model}` : c.model;
}

/**
 * Authoring-time nudge under a node's model field: cheaper, capability-compatible
 * alternatives to the model the user picked. On-demand (a button, not per-keystroke)
 * so typing a model name never spams the backend. Framed as candidates to A/B test —
 * it gates on capability and price, never on quality.
 */
export function ModelRightsizing({
  model,
  needsTools,
  nodeId,
  instruction,
  readOnly = false,
  onPick,
}: {
  model: string;
  /** True when tools are attached — a cheaper model must support function calling. */
  needsTools: boolean;
  /** This node's id — the experiment harvests its audit history by node. */
  nodeId?: string;
  /** The agent's instruction — replayed as the system prompt during the experiment. */
  instruction?: string;
  readOnly?: boolean;
  onPick: (ref: string) => void;
}) {
  const [result, setResult] = useState<RightsizingResult | null>(null);
  const [fetchedFor, setFetchedFor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = model.trim();
  // Authoring aid only, and nothing to compare without a model.
  if (readOnly || !trimmed) return null;

  async function check() {
    setLoading(true);
    setError(null);
    try {
      const res = await getRightsizing({ incumbent: trimmed, needs_tools: needsTools });
      setResult(res);
      setFetchedFor(trimmed);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }

  const stale = fetchedFor !== null && fetchedFor !== trimmed;
  const showTrigger = result === null || stale;

  return (
    <div className="mt-1.5">
      {showTrigger && (
        <button
          type="button"
          onClick={check}
          disabled={loading}
          className="text-xs font-medium text-accent hover:underline disabled:opacity-50"
        >
          {loading ? "Checking pricing…" : stale ? "Re-check for cheaper models" : "Find cheaper models"}
        </button>
      )}

      {error && (
        <p className="mt-1 rounded bg-red-500/10 px-2 py-1 text-xs text-red-700 dark:text-red-400">
          {error}
        </p>
      )}

      {result && !stale && (
        <div className="mt-2 space-y-2 rounded-lg border border-border bg-accent/[0.04] p-2.5">
          {!result.incumbent_known ? (
            <p className="text-xs text-muted">{result.note}</p>
          ) : result.candidates.length === 0 ? (
            <p className="text-xs text-muted">{result.note}</p>
          ) : (
            <>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs font-semibold">Cheaper models that can do this job</span>
                {result.incumbent_blended_per_mtok_usd != null && (
                  <span className="shrink-0 text-[11px] text-muted">
                    now: {perMtok(result.incumbent_blended_per_mtok_usd)}/M tok
                  </span>
                )}
              </div>

              <ul className="space-y-1.5">
                {result.candidates.map((c) => (
                  <li
                    key={candidateRef(c)}
                    className="flex items-center justify-between gap-2 rounded-md border border-border bg-surface px-2 py-1.5"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate font-mono text-xs">{c.model}</span>
                        {c.same_provider && (
                          <span className="shrink-0 rounded bg-accent/10 px-1 text-[10px] font-medium text-accent">
                            same provider
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted">
                        <span className="font-medium text-emerald-600 dark:text-emerald-400">
                          −{Math.round(c.savings_pct)}%
                        </span>
                        <span>·</span>
                        <span>{perMtok(c.blended_per_mtok_usd)}/M tok</span>
                        {c.supports_tools && <span title="Supports tool calling">· 🔧</span>}
                        {c.supports_vision && <span title="Supports vision">· 🖼</span>}
                      </div>
                    </div>
                    <Button
                      variant="default"
                      size="sm"
                      onClick={() => onPick(candidateRef(c))}
                      className="shrink-0"
                    >
                      Use
                    </Button>
                  </li>
                ))}
              </ul>

              <p className="text-[11px] leading-relaxed text-muted">
                Capability-matched &amp; cheaper — worth A/B testing on your real traffic,
                not a guarantee of equal quality. {result.assumption}.
              </p>
            </>
          )}
        </div>
      )}

      {nodeId && instruction && (
        <ExperimentPanel model={trimmed} needsTools={needsTools} nodeId={nodeId} instruction={instruction} />
      )}
    </div>
  );
}

const VERDICT_STYLE: Record<string, string> = {
  confirmed:
    "border-emerald-500/40 bg-emerald-500/[0.08] text-emerald-700 dark:text-emerald-400",
  flagged: "border-amber-500/40 bg-amber-500/[0.08] text-amber-700 dark:text-amber-400",
  none: "border-border bg-zinc-500/[0.06] text-muted",
};

/**
 * The *measured* half: replay this node's real inputs through the cheaper candidates and
 * score whether their output is equivalent to the incumbent's. Runs live model calls, so
 * it's on-demand and honest about sample size — "confirmed" only past the case bar.
 */
function ExperimentPanel({
  model,
  needsTools,
  nodeId,
  instruction,
}: {
  model: string;
  needsTools: boolean;
  nodeId: string;
  instruction: string;
}) {
  const [report, setReport] = useState<ExperimentReport | null>(null);
  const [mode, setMode] = useState<"equivalence" | "correctness">("equivalence");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setLoading(true);
    setError(null);
    try {
      const res = await runRightsizingExperiment({
        node_id: nodeId,
        incumbent: model,
        instruction,
        needs_tools: needsTools,
        mode,
      });
      setReport(res);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }

  const correctness = mode === "correctness";
  return (
    <div className="mt-1.5 border-t border-border pt-2">
      <div className="mb-1.5 inline-flex rounded-md border border-border p-0.5 text-[10px]">
        {(["equivalence", "correctness"] as const).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            className={`rounded px-1.5 py-0.5 font-medium ${
              mode === m ? "bg-accent/15 text-accent" : "text-muted hover:text-primary"
            }`}
          >
            {m === "equivalence" ? "vs. incumbent" : "vs. correct answer"}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={run}
        disabled={loading}
        className="block text-xs font-medium text-accent hover:underline disabled:opacity-50"
      >
        {loading
          ? "Replaying real cases… (live model calls, ~1 min)"
          : report
            ? `Re-run ${correctness ? "correctness" : "equivalence"} experiment`
            : `Measure ${correctness ? "correctness on labeled traffic" : "equivalence on real traffic"}`}
      </button>
      <p className="mt-0.5 text-[11px] text-muted">
        {correctness
          ? "Grades the cheaper models against human-labeled correct answers (attach them via quality verdicts). Catches cases the incumbent itself gets wrong. Needs labeled runs + a provider API key."
          : "Replays this node's recorded inputs through the cheaper models and scores whether their answers match what you run today. Needs run history and a provider API key."}
      </p>

      {error && (
        <p className="mt-1 rounded bg-red-500/10 px-2 py-1 text-xs text-red-700 dark:text-red-400">
          {error}
        </p>
      )}

      {report && (
        <div className="mt-2 space-y-2 rounded-lg border border-border bg-surface p-2.5">
          <div className="flex items-center justify-between gap-2">
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide border ${
                VERDICT_STYLE[report.verdict] ?? VERDICT_STYLE.none
              }`}
            >
              {report.verdict === "confirmed"
                ? "Confirmed"
                : report.verdict === "flagged"
                  ? "Flagged"
                  : "No match"}
            </span>
            {report.cases > 0 && (
              <span className="text-[11px] text-muted">
                {report.cases} case{report.cases === 1 ? "" : "s"} · ceiling{" "}
                {Math.round(report.incumbent_self_equivalence * 100)}%
              </span>
            )}
          </div>

          {report.outcomes.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead className="text-muted">
                  <tr className="text-left">
                    <th className="py-0.5 pr-2 font-medium">Model</th>
                    <th className="py-0.5 pr-2 font-medium">
                      {report.mode === "correctness" ? "Correct." : "Equiv."}
                    </th>
                    <th className="py-0.5 font-medium">$/1k calls</th>
                  </tr>
                </thead>
                <tbody>
                  {report.outcomes.map((o) => {
                    const recommended = report.recommended_model === `${o.provider}/${o.model}`;
                    return (
                      <tr
                        key={`${o.provider}/${o.model}`}
                        className={o.is_incumbent ? "text-muted" : ""}
                      >
                        <td className="py-0.5 pr-2 font-mono">
                          {o.model}
                          {o.is_incumbent && <span className="ml-1 text-[10px]">(current)</span>}
                          {recommended && (
                            <span className="ml-1 rounded bg-emerald-500/15 px-1 text-[10px] font-medium text-emerald-700 dark:text-emerald-400">
                              pick
                            </span>
                          )}
                        </td>
                        <td className="py-0.5 pr-2">
                          {Math.round(o.equivalence_rate * 100)}%
                          {o.cases_errored > 0 && (
                            <span className="ml-1 text-amber-600 dark:text-amber-400" title="some cases errored">
                              !
                            </span>
                          )}
                        </td>
                        <td className="py-0.5">
                          {o.est_cost_per_1k_calls_usd != null
                            ? `$${o.est_cost_per_1k_calls_usd.toFixed(2)}`
                            : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-[11px] leading-relaxed text-muted">{report.note}</p>
        </div>
      )}
    </div>
  );
}
