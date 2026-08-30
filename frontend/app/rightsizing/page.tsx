"use client";

// The Rightsizing & Efficiency screen — where a run's real spend turns into
// concrete "spend less for the same outcome" moves (handoff Govern group).
//
// Four read-only aggregations degrade independently via `useLoad` (inline error /
// empty / skeleton, never a thrown boundary):
//   - Opportunities        getRightsizingOpportunities() -> SpendReport
//   - Unit economics        getUnitEconomics()            -> UnitEconomicsReport
//   - Economic waste        getWaste()                    -> WasteRollup
// plus two on-demand, button-triggered tools that make live backend calls:
//   - Suggest a cheaper model   getRightsizing({...})         -> RightsizingResult
//   - Run an equivalence expt.   runRightsizingExperiment({...}) -> ExperimentReport
// A row in Opportunities can seed either tool (Price / Test), so the flow reads
// top-down: see what's expensive -> price alternatives -> measure equivalence.
//
// Correctness-mode experiments need human-labeled runs; attachQualityVerdict()
// is the labeling affordance, surfaced only in that mode. Authentication uses
// the short-lived HttpOnly session cookie; the exchanged API key is never
// persisted, logged, or placed in a URL.

import { useEffect, useRef, useState } from "react";
import {
  Button,
  Card,
  MonoLabel,
  NODE_TYPE_COLOR,
  Pill,
  Skeleton,
  StatusDot,
} from "@/app/components/primitives";
import { useToast } from "@/app/components/Toast";
import { useLoad, type Loadable } from "@/app/hooks/useLoad";
import {
  attachQualityVerdict,
  errMsg,
  getRightsizing,
  getLatestRightsizingExperiment,
  getRightsizingOpportunities,
  getUnitEconomics,
  getWaste,
  runRightsizingExperiment,
  type CandidateOutcome,
  type ExperimentReport,
  type NodeSpend,
  type RightsizingOption,
  type RightsizingResult,
  type SpendReport,
  type UnitEconomicsReport,
  type WasteRollup,
} from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";
import { fmtUsd } from "@/app/components/ui";
import styles from "./rightsizing.module.css";
import {
  validateExperimentOptions,
  validateSuggestBounds,
  type ExperimentOptions,
} from "./validation";

const MONO = "var(--font-mono)";

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

/** USD per 1M tokens — keep sub-dollar prices legible ($0.150, $0.075) rather
 *  than rounding them to $0.00. Mirrors ModelRightsizing.perMtok. */
function perMtok(usd: number): string {
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;
  return `$${usd.toPrecision(2)}`;
}

/** A 0..1 ratio as a whole-percent string. */
function pctRatio(n: number): string {
  return `${Math.round(n * 100)}%`;
}

/** An already-percent value (e.g. 42.7) as a whole-percent string. */
function pctVal(n: number): string {
  return `${Math.round(n)}%`;
}

/** Measured provider calls are commonly below one tenth of a cent. Preserve
 * enough ledger precision to distinguish them instead of rounding real spend
 * into a misleading zero. */
function fmtExperimentUsd(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "$0.00";
  if (Math.abs(n) >= 0.01) return fmtUsd(n);
  const sign = n < 0 ? "-" : "";
  const rendered = Math.abs(n).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  return `${sign}$${rendered}`;
}

function candidateRef(c: RightsizingOption): string {
  return c.provider ? `${c.provider}/${c.model}` : c.model;
}

function metricsReadError(message: string, surface: "opportunities" | "economics"): string {
  if (!/\b403\b.*forbidden/i.test(message)) return message;
  return surface === "opportunities"
    ? "This role cannot read tenant Rightsizing opportunities. Metrics read permission is required."
    : "This role cannot read tenant economics. Metrics read permission is required.";
}

function experimentError(error: unknown): string {
  const rendered = errMsg(error);
  const status = typeof error === "object" && error !== null && "status" in error
    ? Number((error as { status: unknown }).status)
    : null;
  if (status === 403 || /\b403\b.*forbidden/i.test(rendered)) {
    return (
      "Running measured experiments requires Metrics admin permission. " +
      "Ask a tenant admin or platform admin to run this comparison."
    );
  }
  return rendered;
}

function updateValidatedValue<Field extends string>(
  value: string,
  field: Field,
  invalidField: Field | undefined,
  setValue: (next: string) => void,
  clearOwnedValidation: () => void,
) {
  setValue(value);
  if (invalidField === field) clearOwnedValidation();
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

type SuggestSeed = { incumbent: string; needsTools: boolean; needsVision: boolean };
type ExpSeed = {
  nodeId: string;
  sourceDeploymentRef?: string | null;
  incumbent: string;
  needsTools: boolean;
};

export default function RightsizingPage() {
  const opportunities = useLoad<SpendReport>(getRightsizingOpportunities);
  const econ = useLoad<UnitEconomicsReport>(getUnitEconomics);
  const waste = useLoad<WasteRollup>(getWaste);

  // Read localStorage-derived config only after mount so the static prerender and
  // the first client render agree (no hydration mismatch).
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const connected = mounted && isConfigured();

  // Seeds let an Opportunities row prefill either on-demand tool. Each click mints
  // a fresh object so the target form re-syncs on identity — even for the same
  // node twice. `null` means "never seeded"; the form keeps its own edits then.
  const [suggestSeed, setSuggestSeed] = useState<SuggestSeed | null>(null);
  const [expSeed, setExpSeed] = useState<ExpSeed | null>(null);

  const suggestRef = useRef<HTMLDivElement>(null);
  const expRef = useRef<HTMLDivElement>(null);

  function priceNode(node: NodeSpend) {
    setSuggestSeed({
      incumbent: node.incumbent_model ?? "",
      needsTools: node.uses_tools,
      needsVision: false,
    });
    suggestRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function testNode(node: NodeSpend) {
    setExpSeed({
      nodeId: node.node_id,
      sourceDeploymentRef: node.source_deployment_ref,
      incumbent: node.incumbent_model ?? "",
      needsTools: node.uses_tools,
    });
    expRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function refreshAll() {
    opportunities.reload();
    econ.reload();
    waste.reload();
  }

  return (
    // WebKit can leave a large hydrated page permanently in a blank compositor
    // layer when the whole page owns the transform-based z-fade animation.
    <div style={{ maxWidth: 980, margin: "0 auto", padding: "28px 28px 48px" }}>
      <header
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
          marginBottom: 22,
        }}
      >
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>
            Rightsizing &amp; Efficiency
          </h1>
          <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
            Spend less for the same outcome — capability-matched model swaps, measured equivalence,
            and where the money leaks.
          </p>
        </div>
        {connected && (
          <Button
            variant="neutral"
            onClick={refreshAll}
            disabled={opportunities.loading || econ.loading || waste.loading}
            data-evidence-id="rightsizing.action.refresh"
            style={{ flexShrink: 0 }}
          >
            Refresh
          </Button>
        )}
      </header>

      <div
        role="note"
        data-evidence-id="rightsizing.mode.advisory"
        style={{
          marginBottom: 18,
          padding: "10px 12px",
          border: "1px solid var(--border-subtle)",
          borderRadius: 8,
          background: "var(--surface-raised)",
          color: "var(--text-secondary)",
          fontSize: 12.5,
          lineHeight: 1.45,
        }}
      >
        <strong style={{ color: "var(--text-primary)" }}>Advisory only.</strong>{" "}
        Rightsizing measures and recommends candidates; it never changes a deployed model
        automatically.
      </div>

      {!connected ? (
        <ConnectNote />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <OpportunitiesCard load={opportunities} onPrice={priceNode} onTest={testNode} />

          <div ref={suggestRef}>
            <SuggestCard seed={suggestSeed} connected={connected} />
          </div>

          <div ref={expRef}>
            <ExperimentCard seed={expSeed} connected={connected} />
          </div>

          <UnitEconomicsCard load={econ} />
          <WasteCard load={waste} />
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// 1 · Opportunities — SpendReport.nodes[], ranked by spend x savings potential
// --------------------------------------------------------------------------

function OpportunitiesCard({
  load,
  onPrice,
  onTest,
}: {
  load: Loadable<SpendReport>;
  onPrice: (n: NodeSpend) => void;
  onTest: (n: NodeSpend) => void;
}) {
  return (
    <Card label="Opportunities" pad={16}>
      <p style={cardIntro}>
        Agent nodes ranked by spend. A cheaper model that can still do the job is a candidate to
        test — price it, then measure equivalence on real traffic.
      </p>

      {load.loading && !load.data ? (
        <TableSkeleton rows={4} />
      ) : load.error ? (
        <InlineError
          message={metricsReadError(load.error, "opportunities")}
          onRetry={load.reload}
          evidenceId="rightsizing.opportunities.error"
          retryEvidenceId="rightsizing.opportunities.retry"
        />
      ) : !load.data || load.data.nodes.length === 0 ? (
        <EmptyNote>
          {load.data?.note ?? "No spend attributed yet — needs run history with model costs."}
        </EmptyNote>
      ) : (
        <>
          <div style={{ marginBottom: 12 }}>
            <span style={{ fontSize: 22, fontWeight: 500, fontFamily: "var(--font-sans)", fontVariantNumeric: "tabular-nums" }}>
              {load.data.total_cost_usd > 0
                ? fmtUsd(load.data.total_cost_usd)
                : `${fmtUsd(load.data.total_estimated_cost_usd)} estimated`}
            </span>
            <span style={{ marginLeft: 8, fontSize: 12, color: "var(--text-muted)" }}>
              attributed spend across {load.data.nodes.length} node
              {load.data.nodes.length === 1 ? "" : "s"}
            </span>
          </div>

          <ScrollableRegion
            label="Rightsizing opportunities"
            evidenceId="rightsizing.region.opportunities-scroll"
          >
            <table className={styles.opportunityTable} style={tableStyle}>
              <colgroup>
                <col style={{ width: "16%" }} />
                <col style={{ width: "27%" }} />
                <col style={{ width: "6%" }} />
                <col style={{ width: "8%" }} />
                <col style={{ width: "10%" }} />
                <col style={{ width: "16%" }} />
                <col style={{ width: "8%" }} />
                <col style={{ width: "9%" }} />
              </colgroup>
              <thead>
                <tr>
                  <Th>Node</Th>
                  <Th>Incumbent model</Th>
                  <Th align="right">Runs</Th>
                  <Th align="right">Total</Th>
                  <Th align="right">Mean / call</Th>
                  <Th>Best savings</Th>
                  <Th align="right">Projected</Th>
                  <Th align="right">Action</Th>
                </tr>
              </thead>
              <tbody>
                {load.data.nodes.map((n) => (
                  <OpportunityRow
                    key={`${n.source_deployment_ref ?? "active"}:${n.node_id}`}
                    node={n}
                    onPrice={onPrice}
                    onTest={onTest}
                  />
                ))}
              </tbody>
            </table>
          </ScrollableRegion>
          <p style={noteStyle}>{load.data.note}</p>
        </>
      )}
    </Card>
  );
}

function OpportunityRow({
  node,
  onPrice,
  onTest,
}: {
  node: NodeSpend;
  onPrice: (n: NodeSpend) => void;
  onTest: (n: NodeSpend) => void;
}) {
  const hasSavings = node.best_savings_pct != null;
  const total =
    node.total_cost_usd > 0
      ? node.total_estimated_cost_usd > 0
        ? `${fmtUsd(node.total_cost_usd)} measured · ${fmtUsd(node.total_estimated_cost_usd)} estimated`
        : fmtUsd(node.total_cost_usd)
      : node.total_estimated_cost_usd > 0
        ? `${fmtUsd(node.total_estimated_cost_usd)} estimated`
        : fmtUsd(0);
  const mean =
    node.mean_cost_per_call_usd > 0
      ? node.mean_estimated_cost_per_call_usd > 0
        ? `${fmtUsd(node.mean_cost_per_call_usd)} measured · ${fmtUsd(node.mean_estimated_cost_per_call_usd)} estimated`
        : fmtUsd(node.mean_cost_per_call_usd)
      : node.mean_estimated_cost_per_call_usd > 0
        ? `${fmtUsd(node.mean_estimated_cost_per_call_usd)} estimated`
        : fmtUsd(0);
  const projected =
    node.projected_savings_usd != null && node.projected_savings_usd > 0
      ? node.projected_estimated_savings_usd != null && node.projected_estimated_savings_usd > 0
        ? `≈ ${fmtUsd(node.projected_savings_usd)} measured · ≈ ${fmtUsd(node.projected_estimated_savings_usd)} estimated`
        : `≈ ${fmtUsd(node.projected_savings_usd)}`
      : node.projected_estimated_savings_usd != null
        ? `≈ ${fmtUsd(node.projected_estimated_savings_usd)} estimated`
        : "—";
  const bestSavings = hasSavings
    ? `up to −${pctVal(node.best_savings_pct as number)}`
    : "no cheaper capable model";
  return (
    <tr style={rowStyle}>
      <Td title={node.node_id}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
          <span
            aria-hidden
            style={{
              width: 8,
              height: 8,
              borderRadius: 2,
              background: NODE_TYPE_COLOR.agent,
              flexShrink: 0,
            }}
          />
          <span style={{ fontFamily: MONO, fontSize: 12, color: "var(--text-primary)" }}>
            {node.node_id}
          </span>
        </span>
      </Td>
      <Td title={node.incumbent_model ?? "—"}>
        <span style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-secondary)" }}>
          {node.incumbent_model ?? "—"}
        </span>
      </Td>
      <Td align="right" mono>
        {node.runs}
      </Td>
      <Td align="right" mono title={total}>
        {total}
      </Td>
      <Td align="right" mono title={mean}>
        {mean}
      </Td>
      <Td title={bestSavings}>
        {hasSavings ? (
          <span style={{ color: "var(--success)", fontFamily: MONO, fontSize: 12 }}>
            {bestSavings}
          </span>
        ) : (
          <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>no cheaper capable model</span>
        )}
      </Td>
      <Td align="right" mono title={projected}>
        {projected}
      </Td>
      <Td align="right">
        <span
          style={{ display: "inline-flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}
        >
          {node.incumbent_model && (
            <Button
              variant="neutral"
              onClick={() => onPrice(node)}
              data-evidence-id={`rightsizing.opportunity.${node.node_id}.price`}
              style={{ padding: "4px 9px", fontSize: 11 }}
            >
              Price
            </Button>
          )}
          {node.experiment_ready ? (
            <Button
              variant="primary"
              onClick={() => onTest(node)}
              data-evidence-id={`rightsizing.opportunity.${node.node_id}.test`}
              style={{ padding: "4px 9px", fontSize: 11 }}
            >
              Test
            </Button>
          ) : node.uses_tools ? (
            <Pill tone="muted" style={{ alignSelf: "center" }} title="Tool-using agent — faithful replay not yet supported">
              tools
            </Pill>
          ) : null}
        </span>
      </Td>
    </tr>
  );
}

// --------------------------------------------------------------------------
// 2 · Suggest a cheaper model — getRightsizing (capability + price, no verdict)
// --------------------------------------------------------------------------

function SuggestCard({ seed, connected }: { seed: SuggestSeed | null; connected: boolean }) {
  const [incumbent, setIncumbent] = useState(seed?.incumbent ?? "");
  const [needsTools, setNeedsTools] = useState(seed?.needsTools ?? false);
  const [needsVision, setNeedsVision] = useState(seed?.needsVision ?? false);
  const [minSavings, setMinSavings] = useState("");
  const [limit, setLimit] = useState("");

  const [result, setResult] = useState<RightsizingResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalidBound, setInvalidBound] = useState<"min_savings_pct" | "limit">();

  // Re-sync from an Opportunities "Price" click. Fires on the seed object's
  // identity — each click mints a fresh one — so the user's own edits between
  // clicks are never clobbered by an unrelated re-render.
  useEffect(() => {
    if (!seed) return;
    setIncumbent(seed.incumbent);
    setNeedsTools(seed.needsTools);
    setNeedsVision(seed.needsVision);
    setResult(null);
    setError(null);
    setInvalidBound(undefined);
  }, [seed]);

  const trimmed = incumbent.trim();
  const canSubmit = connected && trimmed.length > 0 && !loading;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    const bounds = validateSuggestBounds(minSavings, limit);
    if (bounds.error) {
      setError(bounds.error);
      setInvalidBound(bounds.invalidField);
      setLoading(false);
      return;
    }
    setInvalidBound(undefined);
    try {
      const body: Parameters<typeof getRightsizing>[0] = {
        incumbent: trimmed,
        needs_tools: needsTools,
        needs_vision: needsVision,
      };
      if (bounds.minSavingsPct != null) body.min_savings_pct = bounds.minSavingsPct;
      if (bounds.limit != null) body.limit = bounds.limit;

      setResult(await getRightsizing(body));
    } catch (err) {
      setResult(null);
      setError(errMsg(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card label="Suggest a cheaper model" pad={16}>
      <p style={cardIntro}>
        Cheaper, capability-compatible alternatives to a model — candidates to A/B test, gated on
        price and capability only, never on quality.
      </p>

      <form
        onSubmit={submit}
        data-evidence-id="rightsizing.suggest.form"
        style={{ display: "flex", flexDirection: "column", gap: 14 }}
      >
        <div style={{ display: "grid", gridTemplateColumns: "1fr 120px 120px", gap: 12 }}>
          <Field label="incumbent" hint="The model you run today (e.g. gpt-4o, claude-sonnet-4).">
            <TextInput
              value={incumbent}
              onChange={setIncumbent}
              placeholder="gpt-4o"
              autoFocus={false}
              required
              evidenceId="rightsizing.suggest.incumbent"
            />
          </Field>
          <Field label="min_savings_%" hint="Optional floor.">
            <TextInput
              value={minSavings}
              onChange={(value) => updateValidatedValue(
                value,
                "min_savings_pct",
                invalidBound,
                setMinSavings,
                () => {
                  setInvalidBound(undefined);
                  setError(null);
                },
              )}
              placeholder="20"
              inputMode="decimal"
              invalid={invalidBound === "min_savings_pct"}
              describedBy="suggest-options-error"
              evidenceId="rightsizing.suggest.min-savings-pct"
            />
          </Field>
          <Field label="limit" hint="Optional cap.">
            <TextInput
              value={limit}
              onChange={(value) => updateValidatedValue(
                value,
                "limit",
                invalidBound,
                setLimit,
                () => {
                  setInvalidBound(undefined);
                  setError(null);
                },
              )}
              placeholder="5"
              inputMode="numeric"
              invalid={invalidBound === "limit"}
              describedBy="suggest-options-error"
              evidenceId="rightsizing.suggest.limit"
            />
          </Field>
        </div>

        <div style={{ display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap" }}>
          <Checkbox
            label="needs tools"
            checked={needsTools}
            onChange={setNeedsTools}
            evidenceId="rightsizing.suggest.needs-tools"
          />
          <Checkbox
            label="needs vision"
            checked={needsVision}
            onChange={setNeedsVision}
            evidenceId="rightsizing.suggest.needs-vision"
          />
          <span
            id="suggest-submit-note"
            style={{ ...noteStyle, margin: "0 0 0 auto" }}
          >
            {canSubmit
              ? "Pricing and capability lookup only · no provider call."
              : "Add the incumbent model to search."}
          </span>
          <Button
            type="submit"
            variant="primary"
            disabled={!canSubmit}
            aria-describedby="suggest-submit-note"
            data-evidence-id="rightsizing.suggest.submit"
          >
            {loading ? "Pricing…" : "Find cheaper models"}
          </Button>
        </div>
      </form>

      {error && (
        <div id="suggest-options-error" style={{ marginTop: 12 }}>
          <InlineError message={error} evidenceId="rightsizing.suggest.error" />
        </div>
      )}

      {result && !error && <SuggestResult result={result} />}
    </Card>
  );
}

function SuggestResult({ result }: { result: RightsizingResult }) {
  if (!result.incumbent_known) {
    return <p style={{ ...noteStyle, marginTop: 14 }}>{result.note}</p>;
  }
  if (result.candidates.length === 0) {
    return <p style={{ ...noteStyle, marginTop: 14 }}>{result.note}</p>;
  }
  return (
    <div style={{ marginTop: 16 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
        <span style={{ fontFamily: MONO, fontSize: 12.5, color: "var(--text-primary)" }}>
          {result.incumbent}
          {result.incumbent_provider && (
            <span style={{ color: "var(--text-faint)" }}> · {result.incumbent_provider}</span>
          )}
        </span>
        {result.incumbent_blended_per_mtok_usd != null && (
          <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
            now: {perMtok(result.incumbent_blended_per_mtok_usd)}/M tok
          </span>
        )}
      </div>

      <ScrollableRegion
        label="Cheaper model candidates"
        evidenceId="rightsizing.region.candidates-scroll"
      >
        <table style={tableStyle}>
          <thead>
            <tr>
              <Th>Candidate</Th>
              <Th>Provider</Th>
              <Th align="right">Blended $/M tok</Th>
              <Th align="right">Savings</Th>
              <Th align="center">Tools</Th>
              <Th align="center">Vision</Th>
            </tr>
          </thead>
          <tbody>
            {result.candidates.map((c) => (
              <tr key={candidateRef(c)} style={rowStyle}>
                <Td>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <span style={{ fontFamily: MONO, fontSize: 12 }}>{c.model}</span>
                    {c.same_provider && <Pill tone="accent">same provider</Pill>}
                  </span>
                </Td>
                <Td>
                  <span style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-secondary)" }}>
                    {c.provider || "—"}
                  </span>
                </Td>
                <Td align="right" mono>
                  {perMtok(c.blended_per_mtok_usd)}
                </Td>
                <Td align="right">
                  <span style={{ color: "var(--success)", fontFamily: MONO, fontSize: 12 }}>
                    −{pctVal(c.savings_pct)}
                  </span>
                </Td>
                <Td align="center">{c.supports_tools ? <Yes /> : <No />}</Td>
                <Td align="center">{c.supports_vision ? <Yes /> : <No />}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollableRegion>

      <p style={noteStyle}>
        Capability-matched &amp; cheaper — worth A/B testing on your real traffic, not a guarantee of
        equal quality. {result.assumption}.
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------
// 3 · Run experiment — runRightsizingExperiment (measured equivalence)
// --------------------------------------------------------------------------

const VERDICT_TONE: Record<ExperimentReport["verdict"], string> = {
  confirmed: "success",
  flagged: "warning",
  none: "muted",
};
const VERDICT_LABEL: Record<ExperimentReport["verdict"], string> = {
  confirmed: "confirmed",
  flagged: "flagged",
  none: "no match",
};

function ExperimentCard({ seed, connected }: { seed: ExpSeed | null; connected: boolean }) {
  const [nodeId, setNodeId] = useState(seed?.nodeId ?? "");
  const [sourceDeploymentRef, setSourceDeploymentRef] = useState(
    seed?.sourceDeploymentRef ?? null,
  );
  const [incumbent, setIncumbent] = useState(seed?.incumbent ?? "");
  const [needsTools, setNeedsTools] = useState(seed?.needsTools ?? false);
  const [needsVision, setNeedsVision] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [mode, setMode] = useState<"equivalence" | "correctness">("equivalence");
  const [judgeModel, setJudgeModel] = useState("");
  const [tolerance, setTolerance] = useState("");
  const [maxCases, setMaxCases] = useState("");
  const [maxCandidates, setMaxCandidates] = useState("");
  const [minCases, setMinCases] = useState("");

  const [report, setReport] = useState<ExperimentReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalidOption, setInvalidOption] = useState<ExperimentOptions["invalidField"]>();

  useEffect(() => {
    if (!seed) return;
    setNodeId(seed.nodeId);
    setSourceDeploymentRef(seed.sourceDeploymentRef ?? null);
    setIncumbent(seed.incumbent);
    setNeedsTools(seed.needsTools);
    setReport(null);
    setError(null);
    setInvalidOption(undefined);
  }, [seed]);

  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    getLatestRightsizingExperiment()
      .then((stored) => {
        if (!cancelled && stored) setReport(stored);
      })
      .catch((err) => {
        if (!cancelled) setError(experimentError(err));
      });
    return () => {
      cancelled = true;
    };
  }, [connected]);

  const canSubmit =
    connected && nodeId.trim().length > 0 && incumbent.trim().length > 0 && instruction.trim().length > 0 && !loading;
  const missingRequirements = [
    !connected && "connect to the API",
    nodeId.trim().length === 0 && "add a node ID",
    incumbent.trim().length === 0 && "add the incumbent model",
    instruction.trim().length === 0 && "add the system prompt",
  ].filter((item): item is string => Boolean(item));
  const disabledReason = loading
    ? "The experiment is already running."
    : `To run this experiment, ${missingRequirements.join(", ")}.`;

  function handleModeKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    current: "equivalence" | "correctness",
  ) {
    const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
    const backward = event.key === "ArrowLeft" || event.key === "ArrowUp";
    if (!forward && !backward && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const next = event.key === "Home"
      ? "equivalence"
      : event.key === "End"
        ? "correctness"
        : current === "equivalence"
          ? "correctness"
          : "equivalence";
    setMode(next);
    event.currentTarget.parentElement
      ?.querySelector<HTMLButtonElement>(`[data-mode="${next}"]`)
      ?.focus();
  }

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    const options = validateExperimentOptions(tolerance, maxCases, maxCandidates, minCases);
    if (options.error) {
      setReport(null);
      setError(options.error);
      setInvalidOption(options.invalidField);
      return;
    }
    setLoading(true);
    setError(null);
    setInvalidOption(undefined);
    try {
      const body: Parameters<typeof runRightsizingExperiment>[0] = {
        node_id: nodeId.trim(),
        incumbent: incumbent.trim(),
        instruction: instruction.trim(),
        needs_tools: needsTools,
        needs_vision: needsVision,
        mode,
      };
      if (sourceDeploymentRef) body.source_deployment_ref = sourceDeploymentRef;
      if (judgeModel.trim()) body.judge_model = judgeModel.trim();
      if (options.tolerancePct != null) body.tolerance_pct = options.tolerancePct;
      if (options.maxCases != null) body.max_cases = options.maxCases;
      if (options.maxCandidates != null) body.max_candidates = options.maxCandidates;
      if (options.minCases != null) body.min_cases = options.minCases;

      setReport(await runRightsizingExperiment(body));
    } catch (err) {
      setReport(null);
      setError(experimentError(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Card label="Run experiment" pad={16}>
      <p style={cardIntro}>
        Replay this node's recorded inputs through the cheaper candidates and score whether their
        output matches the incumbent's. Live model calls — needs run history and a provider API key
        (configured server-side, never entered here).
      </p>

      <form
        onSubmit={run}
        className={styles.experimentForm}
        data-evidence-id="rightsizing.experiment.form"
      >
        <div className={styles.experimentIdentityGrid}>
          <Field label="node_id" hint="The agent node whose audit history is harvested.">
            <TextInput
              value={nodeId}
              onChange={(value) => {
                setNodeId(value);
                if (value !== seed?.nodeId) setSourceDeploymentRef(null);
              }}
              placeholder="answer_node"
              required
              evidenceId="rightsizing.experiment.node-id"
            />
            {sourceDeploymentRef && (
              <span style={{ display: "block", marginTop: 5, fontSize: 11, color: "var(--text-muted)" }}>
                Source deployment: <code>{sourceDeploymentRef}</code>
              </span>
            )}
          </Field>
          <Field label="incumbent" hint="The model it runs today.">
            <TextInput
              value={incumbent}
              onChange={setIncumbent}
              placeholder="gpt-4o"
              required
              evidenceId="rightsizing.experiment.incumbent"
            />
          </Field>
        </div>

        <Field label="instruction" hint="The agent's system prompt — replayed verbatim during the experiment.">
          <TextArea
            value={instruction}
            onChange={setInstruction}
            placeholder="Answer the question using only the provided context."
            required
            evidenceId="rightsizing.experiment.instruction"
          />
        </Field>

        <div className={styles.experimentOptionsGrid}>
          <fieldset className={styles.experimentModeFieldset}>
            <legend className={styles.experimentLegend}>Comparison method</legend>
            <div
              role="radiogroup"
              aria-label="Comparison method"
              className={styles.experimentModeGroup}
            >
              {(["equivalence", "correctness"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={mode === m}
                  tabIndex={mode === m ? 0 : -1}
                  onClick={() => setMode(m)}
                  onKeyDown={(event) => handleModeKeyDown(event, m)}
                  className={styles.experimentModeOption}
                  data-selected={mode === m}
                  data-mode={m}
                  data-evidence-id={`rightsizing.experiment.mode.${m}`}
                >
                  {m === "equivalence" ? "vs. incumbent" : "vs. correct answer"}
                </button>
              ))}
            </div>
            <span className={styles.experimentHelper}>Choose what candidate answers are measured against.</span>
          </fieldset>
          <div className={styles.experimentOptionField}>
            <Field label="Tolerance (%)" hint="Optional difference allowed.">
              <TextInput
                value={tolerance}
                onChange={(value) => updateValidatedValue(
                  value,
                  "tolerance_pct",
                  invalidOption,
                  setTolerance,
                  () => {
                    setInvalidOption(undefined);
                    setError(null);
                  },
                )}
                placeholder="5"
                inputMode="numeric"
                invalid={invalidOption === "tolerance_pct"}
                describedBy="experiment-options-error"
                evidenceId="rightsizing.experiment.tolerance-pct"
              />
            </Field>
          </div>
          <div className={styles.experimentOptionField}>
            <Field label="Maximum cases" hint="Optional replay limit.">
              <TextInput
                value={maxCases}
                onChange={(value) => updateValidatedValue(
                  value,
                  "max_cases",
                  invalidOption,
                  setMaxCases,
                  () => {
                    setInvalidOption(undefined);
                    setError(null);
                  },
                )}
                placeholder="20"
                inputMode="numeric"
                invalid={invalidOption === "max_cases"}
                describedBy="experiment-options-error"
                evidenceId="rightsizing.experiment.max-cases"
              />
            </Field>
          </div>
          <div className={styles.experimentToolsField}>
            <Checkbox
              label="Candidate needs tools"
              checked={needsTools}
              onChange={setNeedsTools}
              evidenceId="rightsizing.experiment.needs-tools"
            />
            <Checkbox
              label="Candidate needs vision"
              checked={needsVision}
              onChange={setNeedsVision}
              evidenceId="rightsizing.experiment.needs-vision"
            />
            <span className={styles.experimentHelper}>Require candidates with the capabilities this node uses.</span>
          </div>
        </div>

        <div className={styles.experimentAdvancedGrid}>
          <Field label="Judge model" hint="Optional model used to compare candidate answers.">
            <TextInput
              value={judgeModel}
              onChange={setJudgeModel}
              placeholder="Provider default"
              evidenceId="rightsizing.experiment.judge-model"
            />
          </Field>
          <Field label="Maximum candidates" hint="Optional whole number from 1 through 6.">
            <TextInput
              value={maxCandidates}
              onChange={(value) => updateValidatedValue(
                value,
                "max_candidates",
                invalidOption,
                setMaxCandidates,
                () => {
                  setInvalidOption(undefined);
                  setError(null);
                },
              )}
              placeholder="3"
              inputMode="numeric"
              invalid={invalidOption === "max_candidates"}
              describedBy="experiment-options-error"
              evidenceId="rightsizing.experiment.max-candidates"
            />
          </Field>
          <Field label="Minimum cases" hint="Optional confirmation floor from 1 through 50.">
            <TextInput
              value={minCases}
              onChange={(value) => updateValidatedValue(
                value,
                "min_cases",
                invalidOption,
                setMinCases,
                () => {
                  setInvalidOption(undefined);
                  setError(null);
                },
              )}
              placeholder="5"
              inputMode="numeric"
              invalid={invalidOption === "min_cases"}
              describedBy="experiment-options-error"
              evidenceId="rightsizing.experiment.min-cases"
            />
          </Field>
        </div>

        <div className={styles.experimentActionFooter}>
          <p
            id={!canSubmit ? "experiment-disabled-reason" : "experiment-mode-note"}
            className={styles.experimentActionNote}
          >
            {!canSubmit
              ? disabledReason
              : mode === "correctness"
                ? "Grades against human-labeled answers. Labeled runs are required."
                : 'Scores against the incumbent. "Confirmed" requires enough matching cases.'}
          </p>
          <Button
            type="submit"
            variant="primary"
            disabled={!canSubmit}
            aria-describedby={!canSubmit ? "experiment-disabled-reason" : "experiment-mode-note"}
            data-evidence-id="rightsizing.experiment.submit"
          >
            {loading ? "Replaying real cases… (~1 min)" : "Run experiment"}
          </Button>
        </div>
      </form>

      {error && (
        <div id="experiment-options-error" style={{ marginTop: 12 }}>
          <InlineError message={error} evidenceId="rightsizing.experiment.error" />
        </div>
      )}

      {report && !error && <ExperimentResult report={report} />}

      {mode === "correctness" && <QualityVerdictForm />}
    </Card>
  );
}

function ExperimentResult({ report }: { report: ExperimentReport }) {
  const equivHeader = report.mode === "correctness" ? "Correctness" : "Equivalence";
  return (
    <div style={{ marginTop: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
        <Pill tone={VERDICT_TONE[report.verdict]}>{VERDICT_LABEL[report.verdict]}</Pill>
        {report.cases > 0 && (
          <span style={{ fontSize: 11.5, color: "var(--text-muted)", fontFamily: MONO }}>
            {report.cases} case{report.cases === 1 ? "" : "s"} · ceiling{" "}
            {pctRatio(report.incumbent_self_equivalence)} · min {report.min_cases}
          </span>
        )}
        {report.recommended_model && (
          <span style={{ fontSize: 11.5, color: "var(--success)", fontFamily: MONO, marginLeft: "auto" }}>
            pick: {report.recommended_model}
          </span>
        )}
      </div>

      {report.outcomes.length > 0 ? (
        <ScrollableRegion
          label="Rightsizing experiment outcomes"
          evidenceId="rightsizing.region.experiment-outcomes-scroll"
        >
          <table style={tableStyle}>
            <thead>
              <tr>
                <Th>Model</Th>
                <Th align="right">{equivHeader}</Th>
                <Th align="right">Savings</Th>
                <Th align="right">$/1k calls</Th>
                <Th align="center">Meets bar</Th>
              </tr>
            </thead>
            <tbody>
              {report.outcomes.map((o) => (
                <OutcomeRow key={`${o.provider}/${o.model}`} outcome={o} recommended={report.recommended_model} />
              ))}
            </tbody>
          </table>
        </ScrollableRegion>
      ) : (
        <EmptyNote>No candidates were evaluated.</EmptyNote>
      )}

      {report.harvest && (
        <p style={{ ...noteStyle, marginTop: 8 }}>
          Harvested {report.harvest.cases} case{report.harvest.cases === 1 ? "" : "s"} · ~
          {Math.round(report.harvest.mean_input_tokens)} in / {Math.round(report.harvest.mean_output_tokens)} out tokens
          {report.harvest.token_profile_measured ? " (measured)" : " (estimated)"}.
        </p>
      )}
      {report.execution && <ExperimentExecution evidence={report.execution} />}
      <p style={noteStyle}>{report.note}</p>
    </div>
  );
}

function ExperimentExecution({
  evidence,
}: {
  evidence: NonNullable<ExperimentReport["execution"]>;
}) {
  return (
    <section
      className={styles.executionEvidence}
      data-evidence-id="rightsizing.experiment.execution-evidence"
      aria-labelledby="rightsizing-execution-heading"
    >
      <div className={styles.executionHeader}>
        <div>
          <h3 id="rightsizing-execution-heading" className={styles.executionTitle}>
            Live execution evidence
          </h3>
          <p className={styles.executionIntro}>
            Runtime and ledger identities returned by this measured experiment.
          </p>
        </div>
        <Pill tone={evidence.provider_call_count > 0 ? "success" : "muted"}>
          {evidence.provider_call_count} live call{evidence.provider_call_count === 1 ? "" : "s"}
        </Pill>
      </div>

      <dl className={styles.executionSummary}>
        <div>
          <dt>Experiment run</dt>
          <dd title={evidence.run_id}>{evidence.run_id}</dd>
        </div>
        {evidence.campaign_id && (
          <div>
            <dt>Campaign</dt>
            <dd title={evidence.campaign_id}>{evidence.campaign_id}</dd>
          </div>
        )}
        <div>
          <dt>Provider spend</dt>
          <dd>{fmtExperimentUsd(evidence.measured_cost_usd)} measured</dd>
        </div>
        <div>
          <dt>Pricing estimate</dt>
          <dd>{fmtExperimentUsd(evidence.estimated_cost_usd)} estimated</dd>
        </div>
      </dl>

      {evidence.calls.length > 0 && (
        <ScrollableRegion
          label="Rightsizing provider call evidence"
          evidenceId="rightsizing.region.experiment-call-evidence-scroll"
        >
          <table className={styles.executionTable} style={tableStyle}>
            <thead>
              <tr>
                <Th>Model</Th>
                <Th>Operation</Th>
                <Th>Provider request</Th>
                <Th>Cost event</Th>
                <Th align="right">Call cost</Th>
                <Th>Cleanup</Th>
              </tr>
            </thead>
            <tbody>
              {evidence.calls.map((call) => {
                const cost = call.measured_cost_usd != null
                  ? `${fmtExperimentUsd(call.measured_cost_usd)} measured`
                  : call.estimated_cost_usd != null
                    ? `${fmtExperimentUsd(call.estimated_cost_usd)} estimated`
                    : "—";
                return (
                  <tr key={call.operation_id} style={rowStyle}>
                    <Td mono>{call.model}</Td>
                    <Td mono title={call.operation_id}>{call.operation_id}</Td>
                    <Td mono title={call.provider_request_id ?? undefined}>
                      {call.provider_request_id ?? "not supplied"}
                    </Td>
                    <Td mono title={call.cost_event_id ?? undefined}>
                      {call.cost_event_id ?? "not recorded"}
                    </Td>
                    <Td align="right" mono>{cost}</Td>
                    <Td>{call.cleanup_status}</Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </ScrollableRegion>
      )}
    </section>
  );
}

function OutcomeRow({
  outcome: o,
  recommended,
}: {
  outcome: CandidateOutcome;
  recommended: string | null;
}) {
  const isPick = recommended === `${o.provider}/${o.model}`;
  return (
    <tr style={{ ...rowStyle, opacity: o.is_incumbent ? 0.7 : 1 }}>
      <Td>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontFamily: MONO, fontSize: 12 }}>{o.model}</span>
          {o.is_incumbent && (
            <span style={{ fontSize: 10, color: "var(--text-faint)" }}>(current)</span>
          )}
          {isPick && <Pill tone="success">pick</Pill>}
        </span>
      </Td>
      <Td align="right" mono>
        {pctRatio(o.equivalence_rate)}
        {o.cases_errored > 0 && (
          <span style={{ color: "var(--warning)", marginLeft: 4 }} title={`${o.cases_errored} case(s) errored`}>
            !
          </span>
        )}
      </Td>
      <Td align="right">
        {o.savings_pct != null ? (
          <span style={{ color: "var(--success)", fontFamily: MONO, fontSize: 12 }}>
            −{pctVal(o.savings_pct)}
          </span>
        ) : (
          <span style={{ color: "var(--text-faint)" }}>—</span>
        )}
      </Td>
      <Td align="right" mono>
        {o.est_cost_per_1k_calls_usd != null ? `$${o.est_cost_per_1k_calls_usd.toFixed(2)}` : "—"}
      </Td>
      <Td align="center">
        {o.is_incumbent ? (
          <span style={{ color: "var(--text-faint)" }}>—</span>
        ) : o.meets_bar ? (
          <Pill tone="success">meets bar</Pill>
        ) : (
          <Pill tone="muted">below bar</Pill>
        )}
      </Td>
    </tr>
  );
}

/** Attach a human verdict to a run so correctness-mode experiments have labeled
 *  traffic to grade against. attachQualityVerdict(QualityVerdictRequest). */
function QualityVerdictForm() {
  const toast = useToast();
  const [runId, setRunId] = useState("");
  const [verdict, setVerdict] = useState<"good" | "bad" | "unknown">("good");
  const [source, setSource] = useState("human:console");
  const [expected, setExpected] = useState("");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);

  const canSubmit = runId.trim().length > 0 && source.trim().length > 0 && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setFeedback(null);
    try {
      await attachQualityVerdict({
        run_id: runId.trim(),
        source: source.trim(),
        verdict,
        detail: detail.trim(),
        expected_output: expected.trim() || null,
      });
      const submittedRunId = runId.trim();
      const message = `Verdict "${verdict}" attached to ${submittedRunId}.`;
      toast(`Verdict "${verdict}" attached to ${submittedRunId.slice(0, 8)}`);
      setFeedback({ kind: "success", message });
      setRunId("");
      setExpected("");
      setDetail("");
    } catch (err) {
      const message = `Attach failed: ${errMsg(err)}`;
      toast(message);
      setFeedback({ kind: "error", message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      data-evidence-id="rightsizing.quality-verdict.region"
      style={{
        marginTop: 16,
        paddingTop: 14,
        borderTop: "1px solid var(--hair)",
      }}
    >
      <MonoLabel style={{ display: "block", marginBottom: 4 }}>Attach a quality verdict</MonoLabel>
      <p style={{ fontSize: 11.5, color: "var(--text-faint)", lineHeight: 1.5, margin: "0 0 12px" }}>
        Label a real run good or bad so correctness experiments have ground truth to grade against.
      </p>
      <form
        onSubmit={submit}
        data-evidence-id="rightsizing.quality-verdict.form"
        style={{ display: "flex", flexDirection: "column", gap: 12 }}
      >
        <div style={{ display: "grid", gridTemplateColumns: "1fr 130px 1fr", gap: 12 }}>
          <Field label="run_id">
            <TextInput
              value={runId}
              onChange={setRunId}
              placeholder="run id"
              required
              evidenceId="rightsizing.quality-verdict.run-id"
            />
          </Field>
          <Field label="verdict">
            <Select
              value={verdict}
              onChange={(v) => setVerdict(v as "good" | "bad" | "unknown")}
              options={["good", "bad", "unknown"]}
              evidenceId="rightsizing.quality-verdict.verdict"
            />
          </Field>
          <Field label="source" hint="Who/what judged it.">
            <TextInput
              value={source}
              onChange={setSource}
              placeholder="human:alice"
              required
              evidenceId="rightsizing.quality-verdict.source"
            />
          </Field>
        </div>
        <Field label="expected_output" hint="Optional — the correct answer for this run.">
          <TextInput
            value={expected}
            onChange={setExpected}
            placeholder="Optional"
            evidenceId="rightsizing.quality-verdict.expected-output"
          />
        </Field>
        <Field label="detail" hint="Optional — a note on the judgement.">
          <TextInput
            value={detail}
            onChange={setDetail}
            placeholder="Optional"
            evidenceId="rightsizing.quality-verdict.detail"
          />
        </Field>
        <div>
          <Button
            type="submit"
            variant="neutral"
            disabled={!canSubmit}
            aria-describedby={!canSubmit ? "quality-verdict-disabled-reason" : undefined}
            data-evidence-id="rightsizing.quality-verdict.submit"
          >
            {busy ? "Attaching…" : "Attach verdict"}
          </Button>
          {!canSubmit && !busy && (
            <span id="quality-verdict-disabled-reason" style={{ ...noteStyle, marginLeft: 10 }}>
              Add a run ID and source to attach a verdict.
            </span>
          )}
        </div>
      </form>
      {feedback && (
        <p
          role={feedback.kind === "error" ? "alert" : "status"}
          aria-live={feedback.kind === "error" ? "assertive" : "polite"}
          data-evidence-id="rightsizing.quality-verdict.feedback"
          style={{
            ...noteStyle,
            color: feedback.kind === "error" ? "var(--danger)" : "var(--success)",
          }}
        >
          {feedback.message}
        </p>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// 4 · Unit economics — getUnitEconomics
// --------------------------------------------------------------------------

function UnitEconomicsCard({ load }: { load: Loadable<UnitEconomicsReport> }) {
  return (
    <Card label="Unit economics" pad={16}>
      <p style={cardIntro}>
        What one successful outcome actually costs — spend on finished runs divided by the ones that
        succeeded, so the price of failures loads onto each good result.
      </p>

      {load.loading && !load.data ? (
        <StatSkeleton />
      ) : load.error ? (
        <InlineError
          message={metricsReadError(load.error, "economics")}
          onRetry={load.reload}
          evidenceId="rightsizing.unit-economics.error"
          retryEvidenceId="rightsizing.unit-economics.retry"
        />
      ) : !load.data ? (
        <EmptyNote>Needs run history.</EmptyNote>
      ) : (
        <UnitEconomicsBody report={load.data} />
      )}
    </Card>
  );
}

function UnitEconomicsBody({ report }: { report: UnitEconomicsReport }) {
  const headline = report.cost_per_successful_run_usd;
  const estimatedHeadline = report.estimated_cost_per_successful_run_usd;
  const noData =
    report.window_runs === 0 ||
    (report.runs_with_cost === 0 && report.runs_with_estimated_cost === 0);
  if (noData) {
    return (
      <>
        <EmptyNote>{report.note}</EmptyNote>
        {report.quality && <QualityCoveragePanel quality={report.quality} />}
      </>
    );
  }

  const headlineValue =
    headline != null
      ? estimatedHeadline != null
        ? `${fmtUsd(headline)} measured · ${fmtUsd(estimatedHeadline)} estimated`
        : fmtUsd(headline)
      : estimatedHeadline != null
        ? `${fmtUsd(estimatedHeadline)} estimated`
        : "—";
  const failureTaxValue =
    report.failure_tax_usd > 0
      ? report.estimated_failure_tax_usd > 0
        ? `${fmtUsd(report.failure_tax_usd)} · ${pctRatio(report.failure_tax_ratio)} measured · ${fmtUsd(report.estimated_failure_tax_usd)} · ${pctRatio(report.estimated_failure_tax_ratio)} estimated`
        : `${fmtUsd(report.failure_tax_usd)} · ${pctRatio(report.failure_tax_ratio)}`
      : report.estimated_failure_tax_usd > 0
        ? `${fmtUsd(report.estimated_failure_tax_usd)} · ${pctRatio(report.estimated_failure_tax_ratio)} estimated`
        : `${fmtUsd(0)} · 0%`;

  return (
    <>
      <p style={{ ...noteStyle, marginTop: 0, marginBottom: 12 }}>
        Over the last {report.window_runs} runs.
      </p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "14px 40px" }}>
        <StatTile label="Cost / successful run" value={headlineValue} emphasis />
        <StatTile label="Success rate" value={pctRatio(report.success_rate)} />
        <StatTile
          label="Failure tax"
          value={failureTaxValue}
          tone={
            report.failure_tax_usd > 0 || report.estimated_failure_tax_usd > 0
              ? "warning"
              : undefined
          }
        />
        <StatTile
          label="Runs (ok / failed / in-flight)"
          value={`${report.successful_runs} / ${report.failed_runs} / ${report.in_flight_runs}`}
        />
      </div>

      {report.failure_tax_usd > 0 && report.mean_cost_per_successful_run_usd != null && (
        <p style={{ ...noteStyle, marginBottom: 0 }}>
          Without the failure tax a success would cost {fmtUsd(report.mean_cost_per_successful_run_usd)}.
        </p>
      )}

      {report.by_workflow.length > 1 && (
        <ScrollableRegion
          label="Unit economics by workflow"
          evidenceId="rightsizing.region.unit-economics-scroll"
          style={{ marginTop: 16 }}
        >
          <MonoLabel style={{ display: "block", marginBottom: 8 }}>By workflow</MonoLabel>
          <table style={tableStyle}>
            <thead>
              <tr>
                <Th>Workflow</Th>
                <Th align="right">Runs</Th>
                <Th align="right">Success</Th>
                <Th align="right">Cost / success</Th>
                <Th align="right">Failure tax</Th>
              </tr>
            </thead>
            <tbody>
              {report.by_workflow.map((w) => (
                <tr key={w.workflow_name} style={rowStyle}>
                  <Td>
                    <span style={{ fontFamily: MONO, fontSize: 12 }}>{w.workflow_name}</span>
                  </Td>
                  <Td align="right" mono>
                    {w.runs}
                  </Td>
                  <Td align="right" mono>
                    {pctRatio(w.success_rate)}
                  </Td>
                  <Td align="right" mono>
                    {w.cost_per_successful_run_usd != null
                      ? fmtUsd(w.cost_per_successful_run_usd)
                      : w.estimated_cost_per_successful_run_usd != null
                        ? `${fmtUsd(w.estimated_cost_per_successful_run_usd)} estimated`
                        : "—"}
                  </Td>
                  <Td align="right" mono>
                    {w.failure_tax_usd > 0
                      ? fmtUsd(w.failure_tax_usd)
                      : w.estimated_failure_tax_usd > 0
                        ? `${fmtUsd(w.estimated_failure_tax_usd)} estimated`
                        : fmtUsd(0)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollableRegion>
      )}

      {report.quality && <QualityCoveragePanel quality={report.quality} />}

      <p style={{ ...noteStyle, marginBottom: 0 }}>{report.note}</p>
    </>
  );
}

function QualityCoveragePanel({
  quality,
}: {
  quality: NonNullable<UnitEconomicsReport["quality"]>;
}) {
  if (quality.labeled_terminal_runs === 0) return null;
  const hasQualityCost =
    quality.state === "ok" && quality.cost_per_quality_success_usd != null;
  return (
    <div
      style={{
        marginTop: 16,
        border: "1px solid var(--hair)",
        borderRadius: 8,
        padding: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
        <span
          style={{
            fontSize: 20,
            fontWeight: 500,
            fontFamily: "var(--font-sans)",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {hasQualityCost
            ? fmtUsd(quality.cost_per_quality_success_usd as number)
            : `${quality.labeled_terminal_runs} labeled`}
        </span>
        <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
          {hasQualityCost ? "cost / good outcome" : "terminal run"}
          {quality.labeled_terminal_runs === 1 ? "" : "s"} · {pctRatio(quality.coverage)} coverage ·{" "}
          {quality.quality_successes} good · via {quality.sources.join(", ")}
        </span>
      </div>
      <p style={{ ...noteStyle, marginBottom: 0 }}>{quality.note}</p>
    </div>
  );
}

// --------------------------------------------------------------------------
// 5 · Economic waste — getWaste
// --------------------------------------------------------------------------

function WasteCard({ load }: { load: Loadable<WasteRollup> }) {
  return (
    <Card label="Economic waste" pad={16}>
      <p style={cardIntro}>
        Spend attributed to structural causes — runs that paid but failed (confirmed waste) and
        loops/retries that may be recoverable (flagged). The two never share a dollar.
      </p>

      {load.loading && !load.data ? (
        <StatSkeleton />
      ) : load.error ? (
        <InlineError
          message={metricsReadError(load.error, "economics")}
          onRetry={load.reload}
          evidenceId="rightsizing.waste.error"
          retryEvidenceId="rightsizing.waste.retry"
        />
      ) : !load.data ? (
        <EmptyNote>Needs run history.</EmptyNote>
      ) : (
        <WasteBody report={load.data} />
      )}
    </Card>
  );
}

function WasteBody({ report }: { report: WasteRollup }) {
  const totalWaste = report.total_confirmed_waste_usd + report.total_flagged_waste_usd;
  const noData = report.window_runs === 0 || report.runs_with_cost === 0 || totalWaste === 0;
  if (noData) return <EmptyNote>{report.note}</EmptyNote>;

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "14px 40px" }}>
        <StatTile
          label="Confirmed waste"
          value={fmtUsd(report.total_confirmed_waste_usd)}
          emphasis
          tone={report.total_confirmed_waste_usd > 0 ? "warning" : undefined}
        />
        <StatTile label="Flagged waste" value={fmtUsd(report.total_flagged_waste_usd)} />
        <StatTile label="Of spend" value={pctRatio(report.waste_ratio)} />
        <StatTile label="Runs with waste" value={`${report.runs_with_waste} / ${report.window_runs}`} />
      </div>

      {report.by_kind.length > 0 && (
        <ScrollableRegion
          label="Economic waste by kind"
          evidenceId="rightsizing.region.waste-kind-scroll"
          style={{ marginTop: 16 }}
        >
          <MonoLabel style={{ display: "block", marginBottom: 8 }}>By kind</MonoLabel>
          <table style={tableStyle}>
            <thead>
              <tr>
                <Th>Kind</Th>
                <Th align="right">Count</Th>
                <Th align="right">Wasted</Th>
              </tr>
            </thead>
            <tbody>
              {report.by_kind.map((k) => (
                <tr key={k.kind} style={rowStyle}>
                  <Td>
                    <span style={{ fontFamily: MONO, fontSize: 12 }}>{k.kind}</span>
                  </Td>
                  <Td align="right" mono>
                    {k.count}
                  </Td>
                  <Td align="right" mono>
                    {fmtUsd(k.wasted_usd)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollableRegion>
      )}

      {report.top_findings.length > 0 && (
        <ScrollableRegion
          label="Top economic waste findings"
          evidenceId="rightsizing.region.waste-findings-scroll"
          style={{ marginTop: 16 }}
        >
          <MonoLabel style={{ display: "block", marginBottom: 8 }}>Top findings</MonoLabel>
          <table style={tableStyle}>
            <thead>
              <tr>
                <Th>Kind</Th>
                <Th>Where</Th>
                <Th align="right">Wasted</Th>
                <Th align="center">Type</Th>
              </tr>
            </thead>
            <tbody>
              {report.top_findings.map((f, i) => (
                <tr key={`${f.run_id}-${f.kind}-${i}`} style={rowStyle}>
                  <Td>
                    <span style={{ fontFamily: MONO, fontSize: 12 }}>{f.kind}</span>
                  </Td>
                  <Td>
                    <span style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-secondary)" }}>
                      {f.node_id ?? f.run_id.slice(0, 12)}
                    </span>
                  </Td>
                  <Td align="right" mono>
                    {fmtUsd(f.wasted_usd)}
                  </Td>
                  <Td align="center">
                    <Pill tone={f.confirmed ? "warning" : "muted"}>
                      {f.confirmed ? "confirmed" : "flagged"}
                    </Pill>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollableRegion>
      )}

      <p style={{ ...noteStyle, marginBottom: 0 }}>{report.note}</p>
    </>
  );
}

// --------------------------------------------------------------------------
// Shared bits — styles + atoms (mirror the Templates / Approvals conventions)
// --------------------------------------------------------------------------

const cardIntro: React.CSSProperties = {
  margin: "0 0 14px",
  fontSize: 12.5,
  color: "var(--text-muted)",
  lineHeight: 1.55,
};

const noteStyle: React.CSSProperties = {
  margin: "12px 0 0",
  fontSize: 11.5,
  color: "var(--text-faint)",
  lineHeight: 1.55,
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  tableLayout: "fixed",
  borderCollapse: "collapse",
  fontSize: 13,
};

const rowStyle: React.CSSProperties = {
  borderBottom: "1px solid var(--hair)",
};

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right" | "center";
}) {
  return (
    <th
      style={{
        textAlign: align,
        fontFamily: "var(--font-sans)",
        fontSize: 10.5,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: "var(--text-muted)",
        padding: "0 10px 8px",
        borderBottom: "1px solid var(--hair-strong)",
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = "left",
  mono = false,
  title,
}: {
  children: React.ReactNode;
  align?: "left" | "right" | "center";
  mono?: boolean;
  title?: string;
}) {
  return (
    <td
      title={title}
      style={{
        textAlign: align,
        padding: "8px 10px",
        color: "var(--text-secondary)",
        fontFamily: mono ? MONO : undefined,
        fontVariantNumeric: mono ? "tabular-nums" : undefined,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </td>
  );
}

function StatTile({
  label,
  value,
  emphasis,
  tone,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
  tone?: "warning";
}) {
  return (
    <div>
      <div
        style={{
          fontFamily: "var(--font-sans)",
          fontVariantNumeric: "tabular-nums",
          fontSize: emphasis ? 26 : 17,
          fontWeight: 500,
          color: tone === "warning" ? "var(--warning)" : "var(--text-primary)",
          lineHeight: 1.15,
        }}
      >
        {value}
      </div>
      <div style={{ marginTop: 3, fontSize: 11.5, color: "var(--text-muted)" }}>{label}</div>
    </div>
  );
}

function Yes() {
  return <StatusDot tone="success" />;
}
function No() {
  return <span style={{ color: "var(--text-disabled)", fontSize: 12 }}>—</span>;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label style={{ display: "block" }}>
      <MonoLabel style={{ display: "block", marginBottom: 5 }}>{label}</MonoLabel>
      {children}
      {hint && (
        <span
          style={{
            display: "block",
            marginTop: 5,
            fontSize: 11,
            color: "var(--text-faint)",
            lineHeight: 1.5,
          }}
        >
          {hint}
        </span>
      )}
    </label>
  );
}

function TextInput({
  value,
  onChange,
  placeholder,
  autoFocus,
  inputMode,
  invalid,
  describedBy,
  evidenceId,
  required,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  inputMode?: "numeric" | "decimal";
  invalid?: boolean;
  describedBy?: string;
  evidenceId: string;
  required?: boolean;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      inputMode={inputMode}
      required={required}
      aria-invalid={invalid || undefined}
      aria-describedby={invalid ? describedBy : undefined}
      data-evidence-id={evidenceId}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: MONO,
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: `1px solid ${invalid ? "var(--danger)" : "var(--hair-strong)"}`,
        borderRadius: 6,
        padding: "8px 10px",
      }}
    />
  );
}

function TextArea({
  value,
  onChange,
  placeholder,
  evidenceId,
  required,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  evidenceId: string;
  required?: boolean;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      required={required}
      rows={4}
      spellCheck={false}
      data-evidence-id={evidenceId}
      style={{
        width: "100%",
        boxSizing: "border-box",
        resize: "vertical",
        fontFamily: MONO,
        fontSize: 12.5,
        lineHeight: 1.7,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "10px 12px",
      }}
    />
  );
}

function Select({
  value,
  onChange,
  options,
  evidenceId,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  evidenceId: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      data-evidence-id={evidenceId}
      style={{
        width: "100%",
        boxSizing: "border-box",
        fontFamily: MONO,
        fontSize: 12.5,
        color: "var(--text-primary)",
        background: "var(--bg-code)",
        border: "1px solid var(--hair-strong)",
        borderRadius: 6,
        padding: "8px 10px",
      }}
    >
      {options.map((o) => (
        <option key={o} value={o} style={{ background: "var(--bg-raised)" }}>
          {o}
        </option>
      ))}
    </select>
  );
}

function Checkbox({
  label,
  checked,
  onChange,
  evidenceId,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  evidenceId: string;
}) {
  return (
    <label
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        cursor: "pointer",
        fontFamily: "var(--font-sans)",
        fontSize: 12,
        color: "var(--text-secondary)",
        userSelect: "none",
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        data-evidence-id={evidenceId}
        style={{ accentColor: "var(--accent)", width: 14, height: 14, cursor: "pointer" }}
      />
      {label}
    </label>
  );
}

function ScrollableRegion({
  label,
  evidenceId,
  children,
  style,
}: {
  label: string;
  evidenceId: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div
      className={styles.scrollableRegion}
      role="region"
      aria-label={label}
      data-evidence-id={evidenceId}
      tabIndex={0}
      style={style}
    >
      {children}
    </div>
  );
}

function TableSkeleton({ rows }: { rows: number }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} height={30} />
      ))}
    </div>
  );
}

function StatSkeleton() {
  return (
    <div style={{ display: "flex", gap: 40, flexWrap: "wrap" }}>
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i}>
          <Skeleton width={110} height={26} />
          <Skeleton width={80} height={12} style={{ marginTop: 6 }} />
        </div>
      ))}
    </div>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 12.5, color: "var(--text-muted)", lineHeight: 1.55 }}>{children}</div>
  );
}

function InlineError({
  message,
  onRetry,
  evidenceId,
  retryEvidenceId,
}: {
  message: string;
  onRetry?: () => void;
  evidenceId: string;
  retryEvidenceId?: string;
}) {
  return (
    <div
      role="alert"
      aria-live="assertive"
      data-evidence-id={evidenceId}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 12,
        background: "rgba(248,113,113,0.08)",
        border: "1px solid rgba(248,113,113,0.3)",
        borderRadius: 8,
        padding: "10px 12px",
      }}
    >
      <span
        style={{
          fontSize: 12.5,
          color: "var(--danger)",
          minWidth: 0,
          lineHeight: 1.45,
        }}
      >
        {message}
      </span>
      {onRetry && (
        <Button
          variant="danger"
          onClick={onRetry}
          data-evidence-id={retryEvidenceId}
          style={{ flexShrink: 0 }}
        >
          Retry
        </Button>
      )}
    </div>
  );
}

function ConnectNote() {
  return (
    <Card pad={20}>
      <div style={{ fontSize: 13, color: "var(--text-muted)" }}>
        Not connected. Open <span style={{ color: "var(--accent)" }}>Connect</span> (bottom-left) to
        set the API base and key, then rightsizing insight loads from your run history.
      </div>
    </Card>
  );
}
