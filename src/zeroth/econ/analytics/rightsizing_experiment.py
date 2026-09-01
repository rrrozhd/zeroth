"""Measured model right-sizing — the replay-equivalence experiment (ECON-RIGHTSIZE-02).

Mode A (``rightsizing.py``) answers "cheaper?" and "capable?" — cheap, static, honest, but
only ever "worth testing". This is the measured half: does a cheaper model actually produce
output *equivalent to the model you already trust*, on your own real traffic?

The trick that makes it buildable without a labeled eval set: **incumbent-as-reference**. The
audit trail already stores each run's real input *and* the incumbent's real output. So we
harvest N real cases, replay each input through a candidate model, and score whether the
candidate's answer is equivalent to what the incumbent produced. No ground truth, no
labeling — the user's bar is literally "as good as what I run now".

Honesty rails, carried from ``waste.py``'s confirmed/flagged discipline:

* **Tool-free only (MVP).** A record where the incumbent called tools can't be reproduced by
  a bare prompt, so a no-tools replay would measure the capability gap, not model quality.
  Those records are excluded and reported as skipped — faithful (agent-runner) replay is a
  later phase.
* **Incumbent as its own candidate.** If the incumbent ran at temperature > 0 it won't even
  reproduce its own stored output, so equivalence has a ceiling below 100%. We replay the
  incumbent too and measure that ceiling; a candidate is judged against *it*, not against a
  fictional 100%.
* **Flagged below K.** A small sample is structurally "flagged: worth testing", never
  "confirmed: switch". The verdict refuses to say "switch" until ``min_cases`` is met.
* **Cost uses the incumbent's real token profile** (harvested ``token_usage``), not the
  replay's — the honest projection of what a swap saves on *your* traffic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.econ.analytics.rightsizing import ModelOption
from zeroth.eval.models import EvalCase, EvalDataset, Score
from zeroth.eval.runner import run_eval
from zeroth.eval.scorers import JudgeVerdict, LLMJudgeScorer
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.runtime.agents.provider import ProviderAdapter, ProviderRequest

# Fields that commonly hold "the answer" in a node's output snapshot. The default agent
# output model is ``{"content": "..."}``; contract-typed nodes vary, so we look for a few
# conventional keys before falling back to a stable JSON dump.
_ANSWER_KEYS = ("content", "output", "text", "answer", "result", "response", "message")

# Fallback token profile when no harvested record carried usage — cost projection still
# works, just flagged as an assumption rather than measured.
_DEFAULT_INPUT_TOKENS = 1000
_DEFAULT_OUTPUT_TOKENS = 300

_SUCCESS_STATUSES = {"completed", "success", "succeeded"}

_EQUIVALENCE_INSTRUCTION = (
    "Two AI systems answered the SAME request. Decide whether Response B is equivalent to "
    "Response A *for the user's purpose* — the same substantive answer, decision, or "
    "information. Wording, formatting, ordering, and paraphrase differences are fine. A "
    "materially different answer, a wrong or missing key fact, or a refusal-versus-answer "
    "is NOT equivalent.\n\n"
    'Respond ONLY with JSON of the form {{"score": <float 0..1>, "rationale": "<short '
    'reason>"}} where 1.0 means fully equivalent and 0.0 means materially different.\n\n'
    "Request:\n{request}\n\nResponse A (reference):\n{reference}\n\nResponse B (candidate):\n"
    "{candidate}"
)


def _answer_text(obj: object) -> str:
    """Normalize an output (dict snapshot or raw string) to its answer text.

    Both sides of the equivalence judgment must be text: the incumbent's ``output_snapshot``
    is a dict, a replay returns a string. This collapses either to the substantive answer so
    the judge compares like with like instead of scoring wrapper-key structure as a
    difference.
    """
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, Mapping):
        for key in _ANSWER_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
        string_values = [v for v in obj.values() if isinstance(v, str) and v]
        if len(string_values) == 1:
            return string_values[0]
        return json.dumps(obj, default=str, sort_keys=True)
    return json.dumps(obj, default=str)


class EquivalenceScorer:
    """LLM judge for symmetric equivalence-to-reference (ECON-RIGHTSIZE-02).

    Distinct from ``LLMJudgeScorer`` (asymmetric graded quality against a rubric): this asks
    a symmetric "are these two responses to the same request equivalent for the user's
    purpose?" and normalizes both sides to text first. A provider failure or unparseable
    verdict yields an *errored* Score (never a silent zero), so a flaky judge can't read as a
    quality regression — the same rule ``run_eval`` applies to errored cases.
    """

    def __init__(
        self,
        provider: ProviderAdapter,
        model_name: str,
        *,
        pass_threshold: float = 0.7,
        name: str = "equivalence",
    ) -> None:
        self.name = name
        self._provider = provider
        self._model_name = model_name
        self._pass_threshold = pass_threshold

    async def score(self, output: object, case: EvalCase) -> Score:
        """Judge whether ``output`` (candidate) is equivalent to ``case.expected`` (incumbent)."""
        prompt = _EQUIVALENCE_INSTRUCTION.format(
            request=json.dumps(case.input, default=str),
            reference=_answer_text(case.expected),
            candidate=_answer_text(output),
        )
        request = ProviderRequest(
            model_name=self._model_name,
            messages=[{"role": "user", "content": prompt}],
            output_model=JudgeVerdict,
        )
        try:
            response = await self._provider.ainvoke(request)
            verdict = LLMJudgeScorer._extract_verdict(response)
        except Exception as exc:
            return Score(scorer=self.name, passed=False, error=f"judge failed: {exc}")
        passed = verdict.score >= self._pass_threshold
        return Score(
            scorer=self.name, value=verdict.score, passed=passed, rationale=verdict.rationale
        )


_CORRECTNESS_INSTRUCTION = (
    "An AI system answered a request. A human reviewer provided the CORRECT answer. Decide "
    "whether the AI's answer is correct — the same substantive answer, decision, or "
    "information as the reviewer's. Wording, formatting, ordering, and paraphrase differences "
    "are fine. A materially different answer, a wrong or missing key fact, or a refusal is NOT "
    "correct.\n\n"
    'Respond ONLY with JSON of the form {{"score": <float 0..1>, "rationale": "<short '
    'reason>"}} where 1.0 means fully correct and 0.0 means wrong.\n\n'
    "Request:\n{request}\n\nCorrect answer (from a human reviewer):\n{reference}\n\n"
    "AI answer:\n{candidate}"
)


class CorrectnessScorer:
    """LLM judge for absolute correctness against a human-provided answer (ECON-RIGHTSIZE-04).

    Unlike :class:`EquivalenceScorer` (candidate vs the incumbent's own output), this grades
    the candidate against the reviewer's CORRECT answer — so a cheaper model is judged on
    whether it is *right*, not merely on whether it matches the model you're replacing. It is
    the honest bar for high-stakes nodes: equivalence inherits the incumbent's mistakes;
    correctness catches them. Same errored-not-zero rail as the equivalence judge.
    """

    def __init__(
        self,
        provider: ProviderAdapter,
        model_name: str,
        *,
        pass_threshold: float = 0.7,
        name: str = "correctness",
    ) -> None:
        self.name = name
        self._provider = provider
        self._model_name = model_name
        self._pass_threshold = pass_threshold

    async def score(self, output: object, case: EvalCase) -> Score:
        """Judge whether ``output`` is correct against ``case.expected`` (the human answer)."""
        prompt = _CORRECTNESS_INSTRUCTION.format(
            request=json.dumps(case.input, default=str),
            reference=_answer_text(case.expected),
            candidate=_answer_text(output),
        )
        request = ProviderRequest(
            model_name=self._model_name,
            messages=[{"role": "user", "content": prompt}],
            output_model=JudgeVerdict,
        )
        try:
            response = await self._provider.ainvoke(request)
            verdict = LLMJudgeScorer._extract_verdict(response)
        except Exception as exc:
            return Score(scorer=self.name, passed=False, error=f"judge failed: {exc}")
        passed = verdict.score >= self._pass_threshold
        return Score(
            scorer=self.name, value=verdict.score, passed=passed, rationale=verdict.rationale
        )


class HarvestStats(BaseModel):
    """What the audit-trail harvest yielded and dropped."""

    model_config = ConfigDict(extra="forbid")

    cases: int = 0
    skipped_not_success: int = 0
    skipped_used_tools: int = 0
    skipped_empty_output: int = 0
    # Records produced by a different model than the incumbent — a node whose model changed
    # over time; including them would compare the incumbent's replay against another model's
    # stored output and depress the ceiling for the wrong reason.
    skipped_other_model: int = 0
    mean_input_tokens: float = float(_DEFAULT_INPUT_TOKENS)
    mean_output_tokens: float = float(_DEFAULT_OUTPUT_TOKENS)
    token_profile_measured: bool = False


def _bare_model(model: str) -> str:
    """Model name without a provider/route prefix (``openai/gpt-4o`` -> ``gpt-4o``)."""
    return model.rsplit("/", maxsplit=1)[-1]


def build_experiment_dataset(
    audits: Sequence[NodeAuditRecord],
    *,
    name: str = "rightsizing",
    incumbent_model: str | None = None,
    max_cases: int | None = None,
) -> tuple[EvalDataset, HarvestStats]:
    """Turn a node's audit records into an equivalence dataset (input + incumbent output).

    Keeps only successful, **tool-free** records with a non-empty output snapshot; each
    becomes an :class:`EvalCase` whose ``input`` is the real per-node input and whose
    ``expected`` is the incumbent's real output. When ``incumbent_model`` is given, records
    a *different* model produced (a node whose model changed over time) are dropped, so the
    self-equivalence ceiling is measured against the incumbent's own outputs — not a mix of
    configs. Also measures the incumbent's mean token profile for the cost projection. Skips
    are counted, never silent.
    """
    incumbent_bare = _bare_model(incumbent_model) if incumbent_model else None
    cases: list[EvalCase] = []
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    stats = HarvestStats()
    for record in audits:
        if record.status not in _SUCCESS_STATUSES:
            stats.skipped_not_success += 1
            continue
        if record.tool_calls:
            stats.skipped_used_tools += 1
            continue
        if not record.output_snapshot:
            stats.skipped_empty_output += 1
            continue
        # Neutral usage is a mixed-model aggregate and cannot be attributed to an
        # incumbent. Different named models are excluded for the same reason.
        if record.token_usage is not None:
            produced_by = record.token_usage.model_name
            if not produced_by or (
                incumbent_bare is not None and _bare_model(produced_by) != incumbent_bare
            ):
                stats.skipped_other_model += 1
                continue
        cases.append(
            EvalCase(
                id=record.audit_id,
                input=dict(record.input_snapshot),
                expected=dict(record.output_snapshot),
            )
        )
        if record.token_usage is not None:
            input_tokens.append(record.token_usage.input_tokens)
            output_tokens.append(record.token_usage.output_tokens)
        if max_cases is not None and len(cases) >= max_cases:
            break

    stats.cases = len(cases)
    if input_tokens:
        stats.mean_input_tokens = sum(input_tokens) / len(input_tokens)
        stats.mean_output_tokens = sum(output_tokens) / len(output_tokens)
        stats.token_profile_measured = True
    return EvalDataset(name=name, cases=cases), stats


def build_labeled_dataset(
    audits: Sequence[NodeAuditRecord],
    expected_by_run: Mapping[str, str],
    *,
    name: str = "rightsizing-correctness",
    incumbent_model: str | None = None,
    max_cases: int | None = None,
) -> tuple[EvalDataset, HarvestStats]:
    """Turn a node's audit records into a CORRECTNESS dataset using human-labeled answers.

    Like :func:`build_experiment_dataset`, but each case's ``expected`` is the reviewer's
    *correct* answer (``expected_by_run[run_id]``) rather than the incumbent's own output — so
    a candidate is graded against ground truth, not the model being replaced. Only tool-free
    records whose run carries a human-provided expected answer become cases; success status is
    NOT required (a labeled run the incumbent got wrong is a valid — and valuable — case).
    """
    incumbent_bare = _bare_model(incumbent_model) if incumbent_model else None
    cases: list[EvalCase] = []
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    stats = HarvestStats()
    for record in audits:
        expected = expected_by_run.get(record.run_id)
        if expected is None:
            continue  # unlabeled — not a correctness case
        if record.tool_calls:
            stats.skipped_used_tools += 1
            continue
        if not record.input_snapshot:
            stats.skipped_empty_output += 1
            continue
        if record.token_usage is not None:
            produced_by = record.token_usage.model_name
            if not produced_by or (
                incumbent_bare is not None and _bare_model(produced_by) != incumbent_bare
            ):
                stats.skipped_other_model += 1
                continue
        cases.append(
            EvalCase(id=record.audit_id, input=dict(record.input_snapshot), expected=expected)
        )
        if record.token_usage is not None:
            input_tokens.append(record.token_usage.input_tokens)
            output_tokens.append(record.token_usage.output_tokens)
        if max_cases is not None and len(cases) >= max_cases:
            break

    stats.cases = len(cases)
    if input_tokens:
        stats.mean_input_tokens = sum(input_tokens) / len(input_tokens)
        stats.mean_output_tokens = sum(output_tokens) / len(output_tokens)
        stats.token_profile_measured = True
    return EvalDataset(name=name, cases=cases), stats


def _make_replay_target(model_name: str, instruction: str, provider: ProviderAdapter):
    """Build an eval target that replays a case input through ``model_name``.

    Simplified replay: ``system=instruction`` + ``user=json(input)``. Faithful to what a
    tool-free agent saw; deliberately does not reconstruct tools/retrieval (hence the
    tool-free harvest). The target is swappable, so faithful agent-runner replay can slot in
    later without touching the scoring/ranking logic.
    """

    async def target(input_payload: dict) -> object:
        response = await provider.ainvoke(
            ProviderRequest(
                model_name=model_name,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": json.dumps(input_payload, default=str)},
                ],
            )
        )
        return response.content

    return target


class CandidateOutcome(BaseModel):
    """One model's measured result in a right-sizing experiment."""

    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str
    is_incumbent: bool = False
    equivalence_rate: float = 0.0
    error_rate: float = 0.0
    cases_evaluated: int = 0
    cases_errored: int = 0
    est_cost_per_1k_calls_usd: float | None = None
    # vs the incumbent's projected cost on the same real token profile (None for incumbent).
    savings_pct: float | None = None
    capability_ok: bool = True
    # Cheaper + capable + equivalence within tolerance of the incumbent's ceiling.
    meets_bar: bool = False


class ExperimentCallEvidence(BaseModel):
    """Credential-free provider identity and economics for one measured call."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    provider_request_id: str | None = None
    cost_event_id: str | None = None
    audit_event_id: str | None = None
    model: str
    cost_measurement: str
    measured_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cleanup_status: str
    provider_call_attempted: bool
    cache_hit: bool


class ExperimentExecutionEvidence(BaseModel):
    """Additive run/cost correlation returned by a live measured experiment."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    campaign_id: str | None = None
    provider_call_count: int = 0
    measured_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    calls: list[ExperimentCallEvidence] = Field(default_factory=list)


class ExperimentReport(BaseModel):
    """Result of a measured right-sizing experiment for one node."""

    model_config = ConfigDict(extra="forbid")

    incumbent: str
    node_id: str | None = None
    # "equivalence" (matches the incumbent's own output — label-free) or "correctness" (matches
    # a human-provided answer — the honest bar for high-stakes nodes).
    mode: str = "equivalence"
    cases: int = 0
    min_cases: int = 5
    tolerance_pct: float = 5.0
    # The incumbent's own rate — the realistic ceiling candidates are judged against. In
    # equivalence mode it's the incumbent's self-consistency; in correctness mode it's the
    # incumbent's own correctness on the labeled cases (it isn't necessarily 100% right either).
    incumbent_self_equivalence: float = 0.0
    mean_input_tokens: float = 0.0
    mean_output_tokens: float = 0.0
    token_profile_measured: bool = False
    harvest: HarvestStats | None = None
    outcomes: list[CandidateOutcome] = Field(default_factory=list)
    recommended_model: str | None = None
    # "confirmed" (>= min_cases), "flagged" (a match but too few cases), "none" (no match /
    # no data) — the same vocabulary as econ waste findings.
    verdict: str = "none"
    note: str = ""
    execution: ExperimentExecutionEvidence | None = None


def _cost_per_1k_calls(option: ModelOption, mean_input: float, mean_output: float) -> float:
    """Projected USD per 1,000 calls at the incumbent's real token profile."""
    per_call = (
        option.input_per_mtok_usd * mean_input + option.output_per_mtok_usd * mean_output
    ) / 1_000_000
    return round(per_call * 1000, 4)


async def _measure_equivalence(
    model_name: str,
    *,
    dataset: EvalDataset,
    instruction: str,
    replay_provider: ProviderAdapter,
    judge_provider: ProviderAdapter,
    judge_model: str,
    mode: str = "equivalence",
) -> tuple[float, float, int, int]:
    """Replay every case through ``model_name`` and score it against the reference.

    In ``equivalence`` mode the reference is the incumbent's own output; in ``correctness``
    mode it is the human-labeled answer. Returns ``(pass_rate, error_rate, cases_evaluated,
    cases_errored)``. Reuses the eval harness: ``run_eval`` maps each input through the replay
    target, the scorer judges it, and errored cases count against the rate (never inflate it).
    """
    target = _make_replay_target(model_name, instruction, replay_provider)
    scorer: EquivalenceScorer | CorrectnessScorer = (
        CorrectnessScorer(judge_provider, judge_model)
        if mode == "correctness"
        else EquivalenceScorer(judge_provider, judge_model)
    )
    report = await run_eval(dataset, target, [scorer])
    return report.pass_rate, report.error_rate, report.total, report.errored_count


async def run_experiment(
    *,
    incumbent: ModelOption,
    candidates: Sequence[ModelOption],
    dataset: EvalDataset,
    instruction: str,
    replay_provider: ProviderAdapter,
    judge_provider: ProviderAdapter,
    judge_model: str,
    mean_input_tokens: float,
    mean_output_tokens: float,
    harvest: HarvestStats | None = None,
    node_id: str | None = None,
    tolerance_pct: float = 5.0,
    min_cases: int = 5,
    mode: str = "equivalence",
) -> ExperimentReport:
    """Run the measured right-sizing experiment and produce a ranked, honest recommendation.

    Replays the harvested cases through the incumbent (to measure the self-equivalence
    ceiling) and each candidate, scores equivalence, projects cost on the incumbent's real
    token profile, and recommends the cheapest capability-compatible candidate whose
    equivalence is within ``tolerance_pct`` of the ceiling. The verdict is **confirmed** only
    at ``>= min_cases`` cases; below that it is **flagged** — a lead to test, not a switch.
    """
    is_corr = mode == "correctness"
    report = ExperimentReport(
        incumbent=incumbent.model,
        node_id=node_id,
        mode=mode,
        cases=len(dataset.cases),
        min_cases=min_cases,
        tolerance_pct=tolerance_pct,
        mean_input_tokens=round(mean_input_tokens, 1),
        mean_output_tokens=round(mean_output_tokens, 1),
        token_profile_measured=harvest.token_profile_measured if harvest else False,
        harvest=harvest,
    )

    if not dataset.cases:
        report.verdict = "none"
        report.note = (
            "No labeled runs (a reviewer's correct answer attached) on record for this node "
            "yet — correctness grading needs ground truth. Attach quality verdicts with an "
            "expected answer, then retry."
            if is_corr
            else "No tool-free successful runs on record for this node yet — the measured "
            "experiment needs real traffic. Run the node a few times, then retry."
        )
        return report

    inc_cost = _cost_per_1k_calls(incumbent, mean_input_tokens, mean_output_tokens)

    # Incumbent first: its self-equivalence is the ceiling everything else is judged against.
    inc_equiv, inc_err, inc_n, inc_nerr = await _measure_equivalence(
        incumbent.ref,
        dataset=dataset,
        instruction=instruction,
        replay_provider=replay_provider,
        judge_provider=judge_provider,
        judge_model=judge_model,
        mode=mode,
    )
    report.incumbent_self_equivalence = round(inc_equiv, 4)
    report.outcomes.append(
        CandidateOutcome(
            model=incumbent.model,
            provider=incumbent.provider,
            is_incumbent=True,
            equivalence_rate=round(inc_equiv, 4),
            error_rate=round(inc_err, 4),
            cases_evaluated=inc_n,
            cases_errored=inc_nerr,
            est_cost_per_1k_calls_usd=inc_cost,
            capability_ok=True,
        )
    )

    # Every incumbent replay errored — the provider is unreachable or a key is missing.
    # Don't dress that up as "nothing is cheaper"; say what actually happened.
    if inc_n > 0 and inc_nerr == inc_n:
        report.verdict = "none"
        report.note = (
            "Every replay call failed — the model provider is unreachable or its API key "
            "is missing. Configure the provider credentials and retry."
        )
        return report

    bar = inc_equiv - tolerance_pct / 100.0
    for candidate in candidates:
        equiv, err, n, nerr = await _measure_equivalence(
            candidate.ref,
            dataset=dataset,
            instruction=instruction,
            replay_provider=replay_provider,
            judge_provider=judge_provider,
            judge_model=judge_model,
            mode=mode,
        )
        cand_cost = _cost_per_1k_calls(candidate, mean_input_tokens, mean_output_tokens)
        savings = round((1.0 - cand_cost / inc_cost) * 100.0, 1) if inc_cost > 0 else None
        # Candidates arrive pre-filtered by Mode A's capability gate, so they're already
        # capability-compatible with the node; the bar here is equivalence + cheaper.
        meets = equiv >= bar and cand_cost < inc_cost
        report.outcomes.append(
            CandidateOutcome(
                model=candidate.model,
                provider=candidate.provider,
                equivalence_rate=round(equiv, 4),
                error_rate=round(err, 4),
                cases_evaluated=n,
                cases_errored=nerr,
                est_cost_per_1k_calls_usd=cand_cost,
                savings_pct=savings,
                capability_ok=True,
                meets_bar=meets,
            )
        )

    eligible = [
        o
        for o in report.outcomes
        if not o.is_incumbent and o.meets_bar and o.est_cost_per_1k_calls_usd is not None
    ]
    if eligible:
        best = min(eligible, key=lambda o: o.est_cost_per_1k_calls_usd)
        report.recommended_model = f"{best.provider}/{best.model}" if best.provider else best.model
        if report.cases >= min_cases:
            report.verdict = "confirmed"
            report.note = (
                (
                    f"{best.model} was correct on {report.cases} labeled cases "
                    f"({best.equivalence_rate:.0%} vs the incumbent's own {inc_equiv:.0%} "
                    f"correctness) at ~{best.savings_pct:.0f}% lower cost. Strong candidate — "
                    "validate tool paths and latency before switching."
                )
                if is_corr
                else (
                    f"{best.model} matched {incumbent.model} on {report.cases} recorded cases "
                    f"({best.equivalence_rate:.0%} vs a {inc_equiv:.0%} self-consistency "
                    f"ceiling) at ~{best.savings_pct:.0f}% lower cost. Strong candidate — "
                    "validate tool paths and latency before switching."
                )
            )
        else:
            metric = "correct" if is_corr else "equivalent"
            unit = "labeled case(s)" if is_corr else "case(s)"
            report.verdict = "flagged"
            report.note = (
                f"{best.model} looks {metric} on {report.cases} {unit}, but that's below "
                f"the {min_cases}-case bar for a confident call — worth testing on more "
                "traffic before switching."
            )
    else:
        ceiling = "correctness" if is_corr else "equivalence"
        report.verdict = "none"
        report.note = (
            f"No cheaper model stayed within {tolerance_pct:.0f}% of {incumbent.model}'s "
            f"{inc_equiv:.0%} {ceiling} ceiling on these cases — it looks right-sized already."
        )

    return report


@dataclass(frozen=True)
class HostedBacktestCase:
    """One ephemeral labeled case at the economic-domain boundary."""

    id: str
    input: dict[str, Any]
    expected: dict[str, Any]


@dataclass(frozen=True)
class HostedBacktestRequest:
    """Provider-independent input for one bounded model substitution."""

    workflow: str
    node_id: str | None
    incumbent_model: str
    candidate_model: str
    instruction: str
    cases: tuple[HostedBacktestCase, ...]


@dataclass(frozen=True)
class HostedBacktestResult:
    """Credential-free measured output returned to the hosting adapter."""

    incumbent_success_rate: float | None = None
    candidate_success_rate: float | None = None
    candidate_error_rate: float | None = None
    savings_pct: float | None = None
    provider_calls: int = 0
    reasons: list[str] = field(default_factory=list)


class HostedModelBacktest:
    """Run correctness replays using service-managed provider credentials."""

    def __init__(self, provider: ProviderAdapter | None = None) -> None:
        self._provider = provider

    async def execute(self, request: HostedBacktestRequest) -> HostedBacktestResult:
        from zeroth.econ.analytics.rightsizing import describe

        incumbent = describe(request.incumbent_model)
        candidate = describe(request.candidate_model)
        if incumbent is None or candidate is None:
            return HostedBacktestResult(
                reasons=["pricing is unavailable for the incumbent or candidate model"]
            )
        dataset = EvalDataset(
            name=f"hosted:{request.workflow}:{request.node_id or 'node'}",
            cases=[
                EvalCase(id=case.id, input=case.input, expected=case.expected)
                for case in request.cases
            ],
        )
        provider = self._provider
        if provider is None:
            from zeroth.runtime.agents.provider import LiteLLMProviderAdapter

            provider = LiteLLMProviderAdapter()
        report = await run_experiment(
            incumbent=incumbent,
            candidates=[candidate],
            dataset=dataset,
            instruction=request.instruction,
            replay_provider=provider,
            judge_provider=provider,
            judge_model=request.incumbent_model,
            mean_input_tokens=1000,
            mean_output_tokens=300,
            node_id=request.node_id,
            min_cases=5,
            mode="correctness",
        )
        incumbent_outcome = next((item for item in report.outcomes if item.is_incumbent), None)
        candidate_outcome = next((item for item in report.outcomes if not item.is_incumbent), None)
        provider_calls = sum(item.cases_evaluated * 2 for item in report.outcomes)
        execution_inconclusive = candidate_outcome is None or (
            candidate_outcome.cases_evaluated > 0
            and candidate_outcome.cases_errored == candidate_outcome.cases_evaluated
        )
        reasons = (
            [report.note or "experiment was inconclusive"] if execution_inconclusive else []
        )
        return HostedBacktestResult(
            incumbent_success_rate=(
                incumbent_outcome.equivalence_rate if incumbent_outcome is not None else None
            ),
            candidate_success_rate=(
                candidate_outcome.equivalence_rate if candidate_outcome is not None else None
            ),
            candidate_error_rate=(
                candidate_outcome.error_rate if candidate_outcome is not None else None
            ),
            savings_pct=candidate_outcome.savings_pct if candidate_outcome is not None else None,
            provider_calls=provider_calls,
            reasons=reasons,
        )
