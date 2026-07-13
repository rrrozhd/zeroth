"""Tests for measured model right-sizing — the replay-equivalence experiment.

Fully offline: replay and judge are `CallableProviderAdapter`s that respond to request
content, so no real LLM is ever called. This exercises harvest filtering, the equivalence
scorer, the incumbent-as-ceiling logic, and the confirmed/flagged/none verdicts.
"""

from __future__ import annotations

import json

import pytest

from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.audit.models import NodeAuditRecord, TokenUsage
from zeroth.core.econ.rightsizing import ModelOption
from zeroth.core.econ.rightsizing_experiment import (
    EquivalenceScorer,
    build_experiment_dataset,
    build_labeled_dataset,
    run_experiment,
)
from zeroth.core.eval.models import EvalCase
from zeroth.core.eval.scorers import JudgeVerdict


def _option(model: str, provider: str, in_price: float, out_price: float, **kw) -> ModelOption:
    blended = (3 * in_price + out_price) / 4
    return ModelOption(
        model=model,
        provider=provider,
        input_per_mtok_usd=in_price,
        output_per_mtok_usd=out_price,
        blended_per_mtok_usd=blended,
        savings_pct=kw.pop("savings_pct", 0.0),
        **kw,
    )


def _audit(
    node_id: str,
    inp: dict,
    out: dict,
    *,
    status: str = "completed",
    tool_calls=None,
    tokens: tuple[int, int] | None = (1000, 200),
    run_id: str = "r1",
) -> NodeAuditRecord:
    from zeroth.core.audit.models import ToolCallRecord

    return NodeAuditRecord(
        audit_id=f"a-{node_id}-{run_id}-{json.dumps(inp, sort_keys=True)}",
        run_id=run_id,
        node_id=node_id,
        graph_version_ref="g1",
        deployment_ref="default",
        status=status,
        input_snapshot=inp,
        output_snapshot=out,
        token_usage=TokenUsage(input_tokens=tokens[0], output_tokens=tokens[1]) if tokens else None,
        tool_calls=[ToolCallRecord(tool_ref=t, alias=t) for t in (tool_calls or [])],
    )


def _replay_provider(answer_for):
    """A replay adapter: maps (model, input) -> answer text via ``answer_for``."""

    def fn(request):
        user = request.messages[-1]["content"]
        inp = json.loads(user)
        return ProviderResponse(content=answer_for(request.model_name, inp))

    return CallableProviderAdapter(fn)


def _equality_judge():
    """A judge adapter that scores 1.0 iff the candidate text equals the reference text.

    Extracts both from the equivalence prompt's known template markers — which also asserts
    the scorer built the prompt with those sections.
    """

    def fn(request):
        prompt = request.messages[0]["content"]
        reference = prompt.split("Response A (reference):\n", 1)[1].split(
            "\n\nResponse B (candidate):\n"
        )[0]
        candidate = prompt.split("Response B (candidate):\n", 1)[1]
        score = 1.0 if reference.strip() == candidate.strip() else 0.0
        return ProviderResponse(content=JudgeVerdict(score=score, rationale="test"))

    return CallableProviderAdapter(fn)


# --- Harvest -------------------------------------------------------------------


def test_harvest_keeps_only_successful_tool_free_records():
    audits = [
        _audit("agent", {"q": "1"}, {"content": "a1"}),
        _audit("agent", {"q": "2"}, {"content": "a2"}, status="failed"),
        _audit("agent", {"q": "3"}, {"content": "a3"}, tool_calls=["search"]),
        _audit("agent", {"q": "4"}, {}),  # empty output
    ]
    dataset, stats = build_experiment_dataset(audits)
    assert stats.cases == 1
    assert stats.skipped_not_success == 1
    assert stats.skipped_used_tools == 1
    assert stats.skipped_empty_output == 1
    assert dataset.cases[0].input == {"q": "1"}
    assert dataset.cases[0].expected == {"content": "a1"}


def test_harvest_measures_token_profile():
    audits = [
        _audit("agent", {"q": "1"}, {"content": "a"}, tokens=(1000, 200)),
        _audit("agent", {"q": "2"}, {"content": "b"}, tokens=(2000, 400)),
    ]
    _, stats = build_experiment_dataset(audits)
    assert stats.token_profile_measured is True
    assert stats.mean_input_tokens == 1500
    assert stats.mean_output_tokens == 300


def test_harvest_falls_back_to_default_token_profile():
    audits = [_audit("agent", {"q": "1"}, {"content": "a"}, tokens=None)]
    _, stats = build_experiment_dataset(audits)
    assert stats.token_profile_measured is False
    assert stats.mean_input_tokens == 1000  # documented default


def test_harvest_respects_max_cases():
    audits = [_audit("agent", {"q": str(i)}, {"content": str(i)}) for i in range(10)]
    dataset, stats = build_experiment_dataset(audits, max_cases=3)
    assert stats.cases == 3


def test_harvest_drops_records_from_a_different_model():
    # Node's model changed over time: two records from gpt-4o, one from an older model.
    a = _audit("agent", {"q": "1"}, {"content": "x"})
    a.token_usage.model_name = "gpt-4o"
    b = _audit("agent", {"q": "2"}, {"content": "y"})
    b.token_usage.model_name = "gpt-4o"
    c = _audit("agent", {"q": "3"}, {"content": "z"})
    c.token_usage.model_name = "gpt-3.5-turbo"
    dataset, stats = build_experiment_dataset([a, b, c], incumbent_model="openai/gpt-4o")
    assert stats.cases == 2
    assert stats.skipped_other_model == 1


def test_harvest_keeps_records_without_a_recorded_model():
    # No producing-model on record → can't tell, so keep it rather than silently drop.
    rec = _audit("agent", {"q": "1"}, {"content": "x"}, tokens=None)
    dataset, stats = build_experiment_dataset([rec], incumbent_model="openai/gpt-4o")
    assert stats.cases == 1
    assert stats.skipped_other_model == 0


# --- Equivalence scorer --------------------------------------------------------


@pytest.mark.asyncio
async def test_equivalence_scorer_passes_on_match_fails_on_diff():
    judge = _equality_judge()
    scorer = EquivalenceScorer(judge, "judge/model")
    case = EvalCase(id="c", input={"q": "x"}, expected={"content": "the answer"})
    same = await scorer.score({"content": "the answer"}, case)
    assert same.passed is True and same.value == 1.0
    diff = await scorer.score({"content": "something else"}, case)
    assert diff.passed is False and diff.value == 0.0


@pytest.mark.asyncio
async def test_equivalence_scorer_records_judge_failure_as_error():
    def boom(request):
        raise RuntimeError("judge down")

    scorer = EquivalenceScorer(CallableProviderAdapter(boom), "judge/model")
    case = EvalCase(id="c", input={"q": "x"}, expected={"content": "a"})
    score = await scorer.score({"content": "a"}, case)
    assert score.errored is True  # never a silent zero-pass


# --- Full experiment -----------------------------------------------------------

_INCUMBENT = _option("premium", "acme", 10.0, 30.0)


def _dataset_of(n: int):
    audits = [_audit("agent", {"q": str(i)}, {"content": f"ANSWER-{i}"}) for i in range(n)]
    return build_experiment_dataset(audits)


@pytest.mark.asyncio
async def test_confirmed_when_cheap_candidate_matches_over_enough_cases():
    dataset, stats = _dataset_of(6)
    good = _option("mini", "acme", 1.0, 3.0, savings_pct=90.0)
    bad = _option("weak", "globex", 0.5, 1.5, savings_pct=95.0)

    # incumbent + "mini" reproduce the stored answer exactly; "weak" diverges.
    def answer_for(model, inp):
        i = inp["q"]
        if "weak" in model:
            return "WRONG"
        return f"ANSWER-{i}"

    report = await run_experiment(
        incumbent=_INCUMBENT,
        candidates=[good, bad],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(answer_for),
        judge_provider=_equality_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        node_id="agent",
        min_cases=5,
    )
    assert report.incumbent_self_equivalence == 1.0
    assert report.verdict == "confirmed"
    assert report.recommended_model == "acme/mini"
    good_outcome = next(o for o in report.outcomes if o.model == "mini")
    assert good_outcome.meets_bar is True
    bad_outcome = next(o for o in report.outcomes if o.model == "weak")
    assert bad_outcome.equivalence_rate == 0.0
    assert bad_outcome.meets_bar is False
    # Cost projected on the incumbent's real token profile (1000 in / 200 out).
    assert good_outcome.est_cost_per_1k_calls_usd == pytest.approx(
        (1.0 * 1000 + 3.0 * 200) / 1e6 * 1000, abs=1e-6
    )


@pytest.mark.asyncio
async def test_flagged_when_match_but_too_few_cases():
    dataset, stats = _dataset_of(2)
    good = _option("mini", "acme", 1.0, 3.0, savings_pct=90.0)

    report = await run_experiment(
        incumbent=_INCUMBENT,
        candidates=[good],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(lambda m, inp: f"ANSWER-{inp['q']}"),
        judge_provider=_equality_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        min_cases=5,
    )
    assert report.recommended_model == "acme/mini"
    assert report.verdict == "flagged"  # match, but below the K bar


@pytest.mark.asyncio
async def test_none_when_no_candidate_matches():
    dataset, stats = _dataset_of(6)
    bad = _option("weak", "globex", 0.5, 1.5, savings_pct=95.0)

    def answer_for(model, inp):
        return f"ANSWER-{inp['q']}" if "premium" in model else "WRONG"

    report = await run_experiment(
        incumbent=_INCUMBENT,
        candidates=[bad],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(answer_for),
        judge_provider=_equality_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        min_cases=5,
    )
    assert report.recommended_model is None
    assert report.verdict == "none"


@pytest.mark.asyncio
async def test_tolerance_is_relative_to_incumbent_ceiling():
    # Incumbent is non-deterministic: it only reproduces its own output half the time,
    # so the ceiling is 0.5. A candidate at 0.5 is within tolerance of THAT, not of 100%.
    dataset, stats = _dataset_of(6)
    good = _option("mini", "acme", 1.0, 3.0, savings_pct=90.0)

    def answer_for(model, inp):
        i = int(inp["q"])
        # both incumbent and mini match on even cases, miss on odd → 50% each
        return f"ANSWER-{i}" if i % 2 == 0 else "drifted"

    report = await run_experiment(
        incumbent=_INCUMBENT,
        candidates=[good],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(answer_for),
        judge_provider=_equality_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        min_cases=5,
        tolerance_pct=5.0,
    )
    assert report.incumbent_self_equivalence == pytest.approx(0.5)
    mini = next(o for o in report.outcomes if o.model == "mini")
    assert mini.equivalence_rate == pytest.approx(0.5)
    assert mini.meets_bar is True  # within 5pts of the 50% ceiling
    assert report.verdict == "confirmed"


@pytest.mark.asyncio
async def test_all_replays_failing_reports_provider_error_not_no_savings():
    dataset, stats = _dataset_of(6)

    def boom(request):
        raise RuntimeError("no API key")

    report = await run_experiment(
        incumbent=_INCUMBENT,
        candidates=[_option("mini", "acme", 1.0, 3.0)],
        dataset=dataset,
        instruction="answer",
        replay_provider=CallableProviderAdapter(boom),
        judge_provider=_equality_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        min_cases=5,
    )
    assert report.verdict == "none"
    assert "provider is unreachable" in report.note
    # Only the incumbent outcome exists — candidates were skipped after the incumbent
    # replay came back all-errored, so we never burned calls on a doomed run.
    assert [o.model for o in report.outcomes] == ["premium"]


@pytest.mark.asyncio
async def test_empty_dataset_yields_none_verdict_with_guidance():
    dataset, stats = build_experiment_dataset([])
    report = await run_experiment(
        incumbent=_INCUMBENT,
        candidates=[_option("mini", "acme", 1.0, 3.0)],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(lambda m, inp: "x"),
        judge_provider=_equality_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
    )
    assert report.verdict == "none"
    assert report.outcomes == []
    assert "real traffic" in report.note


# --- Correctness mode (grade against human-labeled answers) ---------------------


def _correctness_judge():
    """Judge scoring 1.0 iff the AI answer equals the human reference (correctness prompt)."""

    def fn(request):
        prompt = request.messages[0]["content"]
        reference = prompt.split("Correct answer (from a human reviewer):\n", 1)[1].split(
            "\n\nAI answer:\n"
        )[0]
        candidate = prompt.split("AI answer:\n", 1)[1]
        score = 1.0 if reference.strip() == candidate.strip() else 0.0
        return ProviderResponse(content=JudgeVerdict(score=score, rationale="test"))

    return CallableProviderAdapter(fn)


def test_build_labeled_dataset_keeps_only_labeled_tool_free_records():
    audits = [
        _audit("agent", {"q": "1"}, {"content": "a1"}, run_id="r1"),  # labeled
        _audit("agent", {"q": "2"}, {"content": "a2"}, run_id="r2"),  # unlabeled -> dropped
        _audit("agent", {"q": "3"}, {"content": "a3"}, run_id="r3", tool_calls=["t"]),  # tools
    ]
    expected_by_run = {"r1": "RIGHT-1", "r3": "RIGHT-3"}
    dataset, stats = build_labeled_dataset(audits, expected_by_run)
    assert len(dataset.cases) == 1  # r2 unlabeled, r3 uses tools
    assert dataset.cases[0].expected == "RIGHT-1"  # the human answer, not the incumbent output
    assert stats.skipped_used_tools == 1


@pytest.mark.asyncio
async def test_correctness_recommends_candidate_that_beats_the_incumbent():
    # 5 labeled cases. The incumbent is only 80% correct (gets q=4 wrong); the cheap
    # candidate is correct on all 5 -> it is MORE correct AND cheaper -> confirmed switch.
    audits = [
        _audit("agent", {"q": str(i)}, {"content": f"inc-{i}"}, run_id=f"r{i}") for i in range(5)
    ]
    expected_by_run = {f"r{i}": f"RIGHT-{i}" for i in range(5)}
    dataset, stats = build_labeled_dataset(audits, expected_by_run)

    incumbent = _option("premium", "acme", 10.0, 30.0)
    cand = _option("mini", "acme", 1.0, 3.0, savings_pct=90.0)

    def answer_for(model, inp):
        i = inp["q"]
        if "premium" in model and i == "4":
            return "WRONG"  # the incumbent is wrong on one case
        return f"RIGHT-{i}"  # everyone else (incl. the cheap candidate) is correct

    report = await run_experiment(
        incumbent=incumbent,
        candidates=[cand],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(answer_for),
        judge_provider=_correctness_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        node_id="agent",
        min_cases=5,
        mode="correctness",
    )
    assert report.mode == "correctness"
    assert report.incumbent_self_equivalence == 0.8  # the incumbent's own correctness
    cand_outcome = next(o for o in report.outcomes if o.model == "mini")
    assert cand_outcome.equivalence_rate == 1.0  # candidate correct on all 5
    assert report.verdict == "confirmed"
    assert report.recommended_model == "acme/mini"
    assert "correct" in report.note.lower()


@pytest.mark.asyncio
async def test_correctness_with_no_labels_asks_for_verdicts():
    dataset, stats = build_labeled_dataset([_audit("agent", {"q": "1"}, {"content": "x"})], {})
    report = await run_experiment(
        incumbent=_option("premium", "acme", 10.0, 30.0),
        candidates=[_option("mini", "acme", 1.0, 3.0)],
        dataset=dataset,
        instruction="answer",
        replay_provider=_replay_provider(lambda m, i: "x"),
        judge_provider=_correctness_judge(),
        judge_model="judge/model",
        mean_input_tokens=stats.mean_input_tokens,
        mean_output_tokens=stats.mean_output_tokens,
        harvest=stats,
        node_id="agent",
        mode="correctness",
    )
    assert report.verdict == "none"
    assert "labeled" in report.note.lower() or "expected answer" in report.note.lower()
