"""An independent ledger for hosted replay usage, separate from judging cost."""

from decimal import Decimal

import pytest

from zeroth.econ.analytics.rightsizing import ModelOption
from zeroth.econ.plane.backtesting.executor import ManagedBacktestExecutor
from zeroth.econ.plane.backtesting.schemas import BacktestCreate
from zeroth.governance.audit.models import TokenUsage
from zeroth.runtime.agents.provider import ProviderResponse


class UsageProvider:
    def __init__(self, *, candidate_output=50, judge_scale=1, missing_usage=False, fail_candidate=False):
        self.candidate_output = candidate_output
        self.judge_scale = judge_scale
        self.missing_usage = missing_usage
        self.fail_candidate = fail_candidate
        self.calls = 0

    async def ainvoke(self, request):
        self.calls += 1
        judge = request.output_model is not None
        if self.fail_candidate and not judge and request.model_name.endswith("candidate"):
            raise RuntimeError("provider interrupted before usage was available")
        input_tokens = 200 * self.judge_scale if judge else 100
        output_tokens = (
            20 * self.judge_scale if judge else
            self.candidate_output if request.model_name.endswith("candidate") else 50
        )
        return ProviderResponse(
            content='{"verdict": "correct", "rationale": "correct"}' if judge else '{"answer": 1}',
            token_usage=None if self.missing_usage else TokenUsage(
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens, model_name=request.model_name,
            ),
        )


@pytest.fixture
def payload(monkeypatch):
    options = {
        f"openai/{model}": ModelOption(
            model=model, provider="openai", input_per_mtok_usd=rate,
            output_per_mtok_usd=rate, blended_per_mtok_usd=rate, savings_pct=0,
        )
        for model, rate in (("incumbent", 10), ("candidate", 2), ("gpt-5.6-terra", 3))
    }
    options.update({option.model: option for option in tuple(options.values())})
    monkeypatch.setattr("zeroth.econ.analytics.rightsizing.describe", options.get)
    return BacktestCreate(
        workflow="usage-ledger", baseline_version="v1", node_id="answer",
        incumbent_model="openai/incumbent", instruction="Answer the question.",
        candidate={"model": "openai/candidate"},
        cases=[{"id": str(i), "input": {"question": i}, "expected": {"answer": 1}} for i in range(5)],
        constraints={"min_success_rate": 0.8},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_output", [50, 650, 1000])
@pytest.mark.parametrize("judge_scale", [1, 100])
async def test_replay_cost_uses_each_models_usage_and_excludes_judging(payload, candidate_output, judge_scale):
    provider = UsageProvider(candidate_output=candidate_output, judge_scale=judge_scale)
    result = await ManagedBacktestExecutor(provider=provider).execute(payload)

    # Independently authored ledger: five incumbent replays cost $0.0075;
    # five candidate replays at $2/M vary with actual candidate output size.
    incumbent = Decimal("0.0075")
    candidate = Decimal(5 * (100 + candidate_output) * 2) / Decimal(1_000_000)
    judge = Decimal("0.0066") * judge_scale
    assert result.savings_pct == pytest.approx(float((1 - candidate / incumbent) * 100))
    assert result.incumbent_replay_cost_usd == incumbent
    assert result.candidate_replay_cost_usd == candidate
    assert result.judge_cost_usd == judge
    assert result.cost_basis == "rate_card_from_observed_usage"
    assert result.provider_calls == provider.calls == 20
    evidence = result.evaluation_evidence
    assert evidence is not None
    assert evidence.version == "correctness-replay/4"
    assert evidence.incumbent_model == "openai/incumbent"
    assert evidence.judge_model == "openai/gpt-5.6-terra"
    assert result.pricing_snapshot[evidence.judge_model]["input_per_mtok_usd"] == "3.0"
    assert evidence.candidate_model == "openai/candidate"
    assert evidence.parameters == "judge_max_tokens_8192"
    assert evidence.pass_threshold == 1
    assert len(evidence.rubric_sha256) == 64
    assert evidence.rubric_sha256 != "d1cb8f991e0e118b98c473f35adcdd712e9325b0ca6c839271e7d4578d679b0f"
    assert [case.case_index for case in evidence.cases] == list(range(5))
    assert all(case.incumbent.score == case.candidate.score == 1 for case in evidence.cases)
    assert all(case.incumbent.status == case.candidate.status == "passed" for case in evidence.cases)


@pytest.mark.asyncio
async def test_missing_usage_cannot_create_a_savings_projection(payload):
    result = await ManagedBacktestExecutor(provider=UsageProvider(missing_usage=True)).execute(payload)
    assert result.savings_pct is None
    assert result.incumbent_replay_cost_usd is None
    assert result.candidate_replay_cost_usd is None
    assert result.judge_cost_usd is None
    assert result.reasons
    assert result.candidate_success_rate == 1


@pytest.mark.asyncio
async def test_failed_replays_count_attempted_calls_without_inventing_judge_calls(payload):
    provider = UsageProvider(fail_candidate=True)
    result = await ManagedBacktestExecutor(provider=provider).execute(payload)
    assert provider.calls == 15  # Ten incumbent replay/judge calls, five failed candidate calls.
    assert result.provider_calls == provider.calls
    assert result.candidate_replay_cost_usd is None
    assert result.savings_pct is None
    assert result.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["incumbent", "candidate"])
@pytest.mark.parametrize("outcome", ["failed", "unresolved", "judge_error", "replay_error"])
async def test_retained_case_scores_distinguish_wrong_answers_from_missing_evidence(payload, role, outcome):
    from datetime import UTC, datetime
    from zeroth.econ.plane.backtesting.service import decide

    class CaseProvider(UsageProvider):
        last_replay_model = None
        altered = False

        async def ainvoke(self, request):
            response = await super().ainvoke(request)
            judge = request.output_model is not None
            if not judge:
                self.last_replay_model = request.model_name
            if not self.altered and self.last_replay_model == f"openai/{role}":
                if not judge and outcome == "replay_error":
                    self.altered = True
                    raise RuntimeError("private provider detail")
                if judge and outcome != "replay_error":
                    self.altered = True
                    response.content = (
                        '{"verdict": "incorrect", "rationale": "private rationale"}'
                        if outcome == "failed" else
                        '{"verdict": "unresolved", "rationale": "private ambiguity"}'
                        if outcome == "unresolved" else "private malformed judge output"
                    )
            return response

    provider = CaseProvider()
    result = await ManagedBacktestExecutor(provider=provider).execute(payload)
    report = decide(payload, result, digest="bound-request", evaluated_at=datetime.now(UTC))
    # A missing grade is not evidence that an answer was wrong. Even a tolerant
    # quality floor must not turn an evaluation error into a review recommendation.
    assert report.verdict == ("pass" if outcome == "failed" else "abstain")
    evidence = report.evaluation_evidence
    assert evidence is not None
    assert len(evidence.cases) == 5
    score = getattr(evidence.cases[0], role)
    assert score.status == outcome
    assert score.score == (0 if outcome == "failed" else None)
    for side in ("incumbent", "candidate"):
        scores = [getattr(case, side) for case in evidence.cases]
        assert getattr(report, f"{side}_success_rate") == sum(
            score.status == "passed" for score in scores
        ) / len(scores)
    assert report.candidate_error_rate == sum(
        case.candidate.status in {"unresolved", "judge_error", "replay_error"} for case in evidence.cases
    ) / len(evidence.cases)
    assert report.provider_call_credits == provider.calls == (19 if outcome == "replay_error" else 20)
    assert "private" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_all_incumbent_judge_errors_retain_unrun_candidate_without_extra_calls(payload):
    class UnscorableProvider(UsageProvider):
        async def ainvoke(self, request):
            response = await super().ainvoke(request)
            if request.output_model is not None:
                response.content = "private malformed judge output"
            return response

    provider = UnscorableProvider()
    result = await ManagedBacktestExecutor(provider=provider).execute(payload)
    assert result.provider_calls == provider.calls == 10
    assert result.candidate_success_rate is None
    assert result.candidate_error_rate is None
    assert result.reasons
    assert all(case.incumbent.status == "judge_error" for case in result.evaluation_evidence.cases)
    assert all(case.candidate.status == "not_run" for case in result.evaluation_evidence.cases)
    assert all(case.candidate.score is None for case in result.evaluation_evidence.cases)


@pytest.mark.asyncio
async def test_catalog_resolved_bare_model_uses_the_same_pricing_identity(payload):
    payload.incumbent_model = "incumbent"
    payload.candidate = {"model": "candidate"}
    result = await ManagedBacktestExecutor(provider=UsageProvider()).execute(payload)
    assert result.reasons == []
    assert result.savings_pct == 80
    assert result.judge_cost_usd == Decimal("0.0066")


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    TokenUsage(),
    TokenUsage(input_tokens=-1, output_tokens=1, model_name="openai/candidate"),
    TokenUsage(input_tokens=1, output_tokens=1, total_tokens=99, model_name="openai/candidate"),
    TokenUsage(input_tokens=1, output_tokens=1, model_name="unmapped-provider/model"),
])
async def test_ambiguous_or_inconsistent_usage_stays_unresolved(payload, replacement):
    class InvalidUsageProvider(UsageProvider):
        async def ainvoke(self, request):
            response = await super().ainvoke(request)
            if request.output_model is None and request.model_name.endswith("candidate"):
                response.token_usage = replacement
            return response

    result = await ManagedBacktestExecutor(provider=InvalidUsageProvider()).execute(payload)
    assert result.candidate_replay_cost_usd is None
    assert result.savings_pct is None
    assert result.usage_by_role["candidate"]["unresolved_invocations"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_rate", [-1, float("nan"), float("inf")])
async def test_invalid_pricing_abstains_before_any_provider_call(payload, monkeypatch, invalid_rate):
    option = ModelOption(
        model="incumbent", provider="openai", input_per_mtok_usd=invalid_rate,
        output_per_mtok_usd=2, blended_per_mtok_usd=2, savings_pct=0,
    )
    monkeypatch.setattr("zeroth.econ.analytics.rightsizing.describe", lambda _model: option)
    provider = UsageProvider()
    result = await ManagedBacktestExecutor(provider=provider).execute(payload)
    assert result.reasons
    assert result.savings_pct is None
    assert provider.calls == result.provider_calls == 0


@pytest.mark.parametrize("missing_usage", [False, True])
def test_cost_evidence_survives_http_retention_and_exact_retry(payload, tmp_path, monkeypatch, missing_usage):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from zeroth.econ.analytics.service_auth import mint_econ_service_token
    from zeroth.econ.plane.backtesting.api import get_backtest_executor, router
    from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
    from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
    from zeroth.econ.plane.config import settings
    from zeroth.econ.plane.database import Base
    from zeroth.econ.plane.scoped_session import ScopedSession
    from zeroth.platform.storage.scoping import TenantWideScopeContext

    engine = create_engine(f"sqlite:///{tmp_path / 'cost-evidence.db'}")
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def scoped_db():
        with Session(engine) as session:
            yield ScopedSession(session, TenantWideScopeContext(tenant_id="tenant-a"))

    provider = UsageProvider(candidate_output=1000, missing_usage=missing_usage)
    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    app.dependency_overrides[get_backtest_executor] = lambda: ManagedBacktestExecutor(provider=provider)
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(settings, "service_principal_tenant_id", "tenant-a")
    headers = {"Authorization": f"Bearer {mint_econ_service_token()}"}
    client = TestClient(app)
    first = client.post("/v1/backtests", json=payload.model_dump(mode="json"), headers=headers)
    duplicate = client.post("/v1/backtests", json=payload.model_dump(mode="json"), headers=headers)
    assert first.status_code == duplicate.status_code == 200
    assert duplicate.json() == first.json()
    assert client.get("/v1/backtests", headers=headers).json() == [first.json()]
    assert provider.calls == 20
    result = first.json()
    assert result["evaluation_evidence"]["version"] == "correctness-replay/4"
    assert len(result["evaluation_evidence"]["cases"]) == 5
    assert payload.instruction not in first.text
    assert '"case_id"' not in first.text
    assert '"rationale"' not in first.text
    if missing_usage:
        assert result["verdict"] == "abstain"
        assert result["candidate_replay_cost_usd"] is None
    else:
        assert result["verdict"] == "fail"
        assert Decimal(result["candidate_replay_cost_usd"]) == Decimal("0.011")
        assert result["usage_by_role"]["candidate"]["observed_output_tokens"] == 5000
        assert result["pricing_snapshot"]["openai/candidate"]["output_per_mtok_usd"] == "2.0"

    # Older evidence must not acquire the new rubric or version through a read
    # or an exact retry, including when the historical version was omitted.
    old_rubric = "111bcc8c3902da65ae747039b1c02a3e5a40b9034758d5caa9e053bdd64c0b94"
    for stored_version in ("correctness-replay/1", "correctness-replay/2", "correctness-replay/3", None):
        legacy_evidence = {
            **result["evaluation_evidence"],
            "version": stored_version or "correctness-replay/1",
            "rubric_sha256": old_rubric,
            "judge_model": "openai/incumbent",
            "parameters": "provider_defaults",
            "pass_threshold": 0.7,
            "cases": [{
                **case,
                "incumbent": {"status": "passed", "score": 0.8},
                "candidate": {"status": "passed", "score": 0.75},
            } for case in result["evaluation_evidence"]["cases"]],
        }
        legacy_result = {**result, "evaluation_evidence": legacy_evidence}
        with Session(engine) as session:
            record = session.get(EconomicBacktestRecord, result["backtest_id"])
            stored_evidence = dict(legacy_evidence)
            if stored_version is None:
                stored_evidence.pop("version")
            stored_report = {**record.report_json, "evaluation_evidence": stored_evidence}
            record.report_json = stored_report
            session.commit()
        retry = client.post("/v1/backtests", json=payload.model_dump(mode="json"), headers=headers)
        assert retry.status_code == 200
        assert retry.json() == legacy_result
        assert client.get("/v1/backtests", headers=headers).json() == [legacy_result]
        assert provider.calls == 20
        with Session(engine) as session:
            assert session.get(EconomicBacktestRecord, result["backtest_id"]).report_json == stored_report
