"""Content-free evidence retained from a hosted correctness replay."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BacktestCaseScore(BaseModel):
    """One observed grade or an explicit missing-evaluation category."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["passed", "failed", "replay_error", "judge_error", "not_run"]
    score: float | None = Field(default=None, ge=0, le=1)


class BacktestCaseEvidence(BaseModel):
    """Paired scores, indexed into the order bound by the request digest."""

    model_config = ConfigDict(extra="forbid")

    case_index: int = Field(ge=0, lt=25)
    incumbent: BacktestCaseScore
    candidate: BacktestCaseScore


class BacktestEvaluationEvidence(BaseModel):
    """Submitted evaluator settings and numeric results, without customer content."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["correctness-replay/1", "correctness-replay/2"] = "correctness-replay/1"
    incumbent_model: str
    candidate_model: str
    judge_model: str
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pass_threshold: float = Field(ge=0, le=1)
    parameters: Literal["provider_defaults"] = "provider_defaults"
    cases: list[BacktestCaseEvidence] = Field(max_length=25)
