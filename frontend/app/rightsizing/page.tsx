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
// is the labeling affordance, surfaced only in that mode. The API key lives in
// localStorage (lib/config) — it is never logged and never placed in a URL.

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

/** A spend figure: cents matter under $1, dollars above. */
function fmtUsd(n: number): string {
  return n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

/** A 0..1 ratio as a whole-percent string. */
function pctRatio(n: number): string {
  return `${Math.round(n * 100)}%`;
}

/** An already-percent value (e.g. 42.7) as a whole-percent string. */
function pctVal(n: number): string {
  return `${Math.round(n)}%`;
}

function candidateRef(c: RightsizingOption): string {
  return c.provider ? `${c.provider}/${c.model}` : c.model;
}

// --------------------------------------------------------------------------
// Page shell
// --------------------------------------------------------------------------

type SuggestSeed = { incumbent: string; needsTools: boolean; needsVision: boolean };
type ExpSeed = { nodeId: string; incumbent: string; needsTools: boolean };

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
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
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
            style={{ flexShrink: 0 }}
          >
            Refresh
          </Button>
        )}
      </header>

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
        <InlineError message={load.error} onRetry={load.reload} />
      ) : !load.data || load.data.nodes.length === 0 ? (
        <EmptyNote>
          {load.data?.note ?? "No spend attributed yet — needs run history with model costs."}
        </EmptyNote>
      ) : (
        <>
          <div style={{ marginBottom: 12 }}>
            <span style={{ fontSize: 22, fontWeight: 600, fontFamily: MONO }}>
              {fmtUsd(load.data.total_cost_usd)}
            </span>
            <span style={{ marginLeft: 8, fontSize: 12, color: "var(--text-muted)" }}>
              attributed spend across {load.data.nodes.length} node
              {load.data.nodes.length === 1 ? "" : "s"}
            </span>
          </div>

          <div style={{ overflowX: "auto" }}>
            <table style={tableStyle}>
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
                  <OpportunityRow key={n.node_id} node={n} onPrice={onPrice} onTest={onTest} />
                ))}
              </tbody>
            </table>
          </div>
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
  return (
    <tr style={rowStyle}>
      <Td>
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
      <Td>
        <span style={{ fontFamily: MONO, fontSize: 11.5, color: "var(--text-secondary)" }}>
          {node.incumbent_model ?? "—"}
        </span>
      </Td>
      <Td align="right" mono>
        {node.runs}
      </Td>
      <Td align="right" mono>
        {fmtUsd(node.total_cost_usd)}
      </Td>
      <Td align="right" mono>
        {fmtUsd(node.mean_cost_per_call_usd)}
      </Td>
      <Td>
        {hasSavings ? (
          <span style={{ color: "var(--success)", fontFamily: MONO, fontSize: 12 }}>
            up to −{pctVal(node.best_savings_pct as number)}
          </span>
        ) : (
          <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>no cheaper capable model</span>
        )}
      </Td>
      <Td align="right" mono>
        {node.projected_savings_usd != null ? `≈ ${fmtUsd(node.projected_savings_usd)}` : "—"}
      </Td>
      <Td align="right">
        <span
          style={{ display: "inline-flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}
        >
          {node.incumbent_model && (
            <Button
              variant="neutral"
              onClick={() => onPrice(node)}
              style={{ padding: "4px 9px", fontSize: 11 }}
            >
              Price
            </Button>
          )}
          {node.experiment_ready ? (
            <Button
              variant="primary"
              onClick={() => onTest(node)}
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
  }, [seed]);

  const trimmed = incumbent.trim();
  const canSubmit = connected && trimmed.length > 0 && !loading;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    try {
      const body: Parameters<typeof getRightsizing>[0] = {
        incumbent: trimmed,
        needs_tools: needsTools,
        needs_vision: needsVision,
      };
      const minPct = Number(minSavings.trim());
      if (minSavings.trim() && Number.isFinite(minPct)) body.min_savings_pct = minPct;
      const lim = Number(limit.trim());
      if (limit.trim() && Number.isInteger(lim) && lim > 0) body.limit = lim;

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

      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 120px 120px", gap: 12 }}>
          <Field label="incumbent" hint="The model you run today (e.g. gpt-4o, claude-sonnet-4).">
            <TextInput
              value={incumbent}
              onChange={setIncumbent}
              placeholder="gpt-4o"
              autoFocus={false}
            />
          </Field>
          <Field label="min_savings_%" hint="Optional floor.">
            <TextInput value={minSavings} onChange={setMinSavings} placeholder="20" inputMode="numeric" />
          </Field>
          <Field label="limit" hint="Optional cap.">
            <TextInput value={limit} onChange={setLimit} placeholder="5" inputMode="numeric" />
          </Field>
        </div>

        <div style={{ display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap" }}>
          <Checkbox label="needs tools" checked={needsTools} onChange={setNeedsTools} />
          <Checkbox label="needs vision" checked={needsVision} onChange={setNeedsVision} />
          <Button type="submit" variant="primary" disabled={!canSubmit} style={{ marginLeft: "auto" }}>
            {loading ? "Pricing…" : "Find cheaper models"}
          </Button>
        </div>
      </form>

      {error && <div style={{ marginTop: 12 }}><InlineError message={error} /></div>}

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

      <div style={{ overflowX: "auto" }}>
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
      </div>

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
  const [incumbent, setIncumbent] = useState(seed?.incumbent ?? "");
  const [needsTools, setNeedsTools] = useState(seed?.needsTools ?? false);
  const [instruction, setInstruction] = useState("");
  const [mode, setMode] = useState<"equivalence" | "correctness">("equivalence");
  const [tolerance, setTolerance] = useState("");
  const [maxCases, setMaxCases] = useState("");

  const [report, setReport] = useState<ExperimentReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!seed) return;
    setNodeId(seed.nodeId);
    setIncumbent(seed.incumbent);
    setNeedsTools(seed.needsTools);
    setReport(null);
    setError(null);
  }, [seed]);

  const canSubmit =
    connected && nodeId.trim().length > 0 && incumbent.trim().length > 0 && instruction.trim().length > 0 && !loading;

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    try {
      const body: Parameters<typeof runRightsizingExperiment>[0] = {
        node_id: nodeId.trim(),
        incumbent: incumbent.trim(),
        instruction: instruction.trim(),
        needs_tools: needsTools,
        mode,
      };
      const tol = Number(tolerance.trim());
      if (tolerance.trim() && Number.isFinite(tol)) body.tolerance_pct = tol;
      const mc = Number(maxCases.trim());
      if (maxCases.trim() && Number.isInteger(mc) && mc > 0) body.max_cases = mc;

      setReport(await runRightsizingExperiment(body));
    } catch (err) {
      setReport(null);
      setError(errMsg(err));
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

      <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Field label="node_id" hint="The agent node whose audit history is harvested.">
            <TextInput value={nodeId} onChange={setNodeId} placeholder="answer_node" />
          </Field>
          <Field label="incumbent" hint="The model it runs today.">
            <TextInput value={incumbent} onChange={setIncumbent} placeholder="gpt-4o" />
          </Field>
        </div>

        <Field label="instruction" hint="The agent's system prompt — replayed verbatim during the experiment.">
          <TextArea
            value={instruction}
            onChange={setInstruction}
            placeholder="Answer the question using only the provided context."
          />
        </Field>

        <div style={{ display: "flex", gap: 16, alignItems: "flex-end", flexWrap: "wrap" }}>
          <div>
            <MonoLabel style={{ display: "block", marginBottom: 5 }}>mode</MonoLabel>
            <div style={{ display: "inline-flex", border: "1px solid var(--hair-strong)", borderRadius: 6, padding: 2 }}>
              {(["equivalence", "correctness"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  style={{
                    fontFamily: MONO,
                    fontSize: 11,
                    fontWeight: 500,
                    padding: "5px 10px",
                    borderRadius: 4,
                    border: "none",
                    cursor: "pointer",
                    background: mode === m ? "rgba(94,234,212,0.15)" : "transparent",
                    color: mode === m ? "var(--accent)" : "var(--text-muted)",
                  }}
                >
                  {m === "equivalence" ? "vs. incumbent" : "vs. correct answer"}
                </button>
              ))}
            </div>
          </div>
          <div style={{ width: 120 }}>
            <Field label="tolerance_%" hint="Optional.">
              <TextInput value={tolerance} onChange={setTolerance} placeholder="5" inputMode="numeric" />
            </Field>
          </div>
          <div style={{ width: 120 }}>
            <Field label="max_cases" hint="Optional.">
              <TextInput value={maxCases} onChange={setMaxCases} placeholder="20" inputMode="numeric" />
            </Field>
          </div>
          <Checkbox label="needs tools" checked={needsTools} onChange={setNeedsTools} />
          <Button type="submit" variant="primary" disabled={!canSubmit} style={{ marginLeft: "auto" }}>
            {loading ? "Replaying real cases… (~1 min)" : "Run experiment"}
          </Button>
        </div>
      </form>

      <p style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5, marginTop: 8 }}>
        {mode === "correctness"
          ? "Grades candidates against human-labeled correct answers — catches cases the incumbent itself gets wrong. Needs labeled runs (attach verdicts below)."
          : "Scores whether candidates answer the way you run today. \"Confirmed\" only past the case bar."}
      </p>

      {error && <div style={{ marginTop: 12 }}><InlineError message={error} /></div>}

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
        <div style={{ overflowX: "auto" }}>
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
        </div>
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
      <p style={noteStyle}>{report.note}</p>
    </div>
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

  const canSubmit = runId.trim().length > 0 && source.trim().length > 0 && !busy;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    try {
      await attachQualityVerdict({
        run_id: runId.trim(),
        source: source.trim(),
        verdict,
        detail: detail.trim(),
        expected_output: expected.trim() || null,
      });
      toast(`Verdict "${verdict}" attached to ${runId.trim().slice(0, 8)}`);
      setRunId("");
      setExpected("");
      setDetail("");
    } catch (err) {
      toast(`Attach failed: ${errMsg(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
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
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 130px 1fr", gap: 12 }}>
          <Field label="run_id">
            <TextInput value={runId} onChange={setRunId} placeholder="run id" />
          </Field>
          <Field label="verdict">
            <Select
              value={verdict}
              onChange={(v) => setVerdict(v as "good" | "bad" | "unknown")}
              options={["good", "bad", "unknown"]}
            />
          </Field>
          <Field label="source" hint="Who/what judged it.">
            <TextInput value={source} onChange={setSource} placeholder="human:alice" />
          </Field>
        </div>
        <Field label="expected_output" hint="Optional — the correct answer for this run.">
          <TextInput value={expected} onChange={setExpected} placeholder="Optional" />
        </Field>
        <Field label="detail" hint="Optional — a note on the judgement.">
          <TextInput value={detail} onChange={setDetail} placeholder="Optional" />
        </Field>
        <div>
          <Button type="submit" variant="neutral" disabled={!canSubmit}>
            {busy ? "Attaching…" : "Attach verdict"}
          </Button>
        </div>
      </form>
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
        <InlineError message={load.error} onRetry={load.reload} />
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
  const noData = report.window_runs === 0 || report.runs_with_cost === 0 || headline == null;
  if (noData) return <EmptyNote>{report.note}</EmptyNote>;

  return (
    <>
      <p style={{ ...noteStyle, marginTop: 0, marginBottom: 12 }}>
        Over the last {report.window_runs} runs.
      </p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "14px 40px" }}>
        <StatTile label="Cost / successful run" value={fmtUsd(headline as number)} emphasis />
        <StatTile label="Success rate" value={pctRatio(report.success_rate)} />
        <StatTile
          label="Failure tax"
          value={`${fmtUsd(report.failure_tax_usd)} · ${pctRatio(report.failure_tax_ratio)}`}
          tone={report.failure_tax_usd > 0 ? "warning" : undefined}
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
        <div style={{ marginTop: 16, overflowX: "auto" }}>
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
                    {w.cost_per_successful_run_usd != null ? fmtUsd(w.cost_per_successful_run_usd) : "—"}
                  </Td>
                  <Td align="right" mono>
                    {fmtUsd(w.failure_tax_usd)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {report.quality && report.quality.state === "ok" && report.quality.cost_per_quality_success_usd != null && (
        <div
          style={{
            marginTop: 16,
            border: "1px solid var(--hair)",
            borderRadius: 8,
            padding: 12,
          }}
        >
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
            <span style={{ fontSize: 20, fontWeight: 600, fontFamily: MONO }}>
              {fmtUsd(report.quality.cost_per_quality_success_usd)}
            </span>
            <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>
              cost / good outcome · {pctRatio(report.quality.coverage)} labeled · via{" "}
              {report.quality.sources.join(", ")}
            </span>
          </div>
          <p style={{ ...noteStyle, marginBottom: 0 }}>
            A stricter success — only runs a reviewer judged good.{" "}
            {pctRatio(report.quality.quality_success_rate_over_labeled)} of labeled runs were good.
          </p>
        </div>
      )}

      <p style={{ ...noteStyle, marginBottom: 0 }}>{report.note}</p>
    </>
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
        <InlineError message={load.error} onRetry={load.reload} />
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
        <div style={{ marginTop: 16, overflowX: "auto" }}>
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
        </div>
      )}

      {report.top_findings.length > 0 && (
        <div style={{ marginTop: 16, overflowX: "auto" }}>
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
        </div>
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
        fontFamily: MONO,
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
}: {
  children: React.ReactNode;
  align?: "left" | "right" | "center";
  mono?: boolean;
}) {
  return (
    <td
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
          fontFamily: MONO,
          fontVariantNumeric: "tabular-nums",
          fontSize: emphasis ? 26 : 17,
          fontWeight: 600,
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
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  autoFocus?: boolean;
  inputMode?: "numeric";
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      autoFocus={autoFocus}
      inputMode={inputMode}
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
        outline: "none",
      }}
    />
  );
}

function TextArea({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={4}
      spellCheck={false}
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
        outline: "none",
      }}
    />
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
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
        outline: "none",
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
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        cursor: "pointer",
        fontFamily: MONO,
        fontSize: 12,
        color: "var(--text-secondary)",
        userSelect: "none",
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        style={{ accentColor: "var(--accent)", width: 14, height: 14, cursor: "pointer" }}
      />
      {label}
    </label>
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

function InlineError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div
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
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {message}
      </span>
      {onRetry && (
        <Button variant="danger" onClick={onRetry} style={{ flexShrink: 0 }}>
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
