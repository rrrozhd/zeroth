"""Data models for the agent evaluation harness (EVAL-01).

An :class:`EvalDataset` is a named list of :class:`EvalCase` reference cases (input
plus expected/graded output). Running it produces an :class:`EvalReport` of
:class:`CaseResult` s, each carrying the actual output and one :class:`Score` per
scorer.

Errors are first-class and never collapse into a quality verdict: a scorer that
*fails* (e.g. an LLM judge that times out or returns garbage) records ``Score.error``,
and a target that fails to produce output records ``CaseResult.error``. Errored cases
count as *not passed* in the aggregates so a flaky harness can never inflate the
pass rate — see :class:`EvalReport`.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvalCase(BaseModel):
    """One reference case: an input, an optional expected output, and grading hints."""

    model_config = ConfigDict(extra="forbid")

    id: str
    input: dict[str, Any]
    expected: Any = None
    # Per-case rubric override for an LLM judge (falls back to the scorer's default).
    rubric: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalDataset(BaseModel):
    """A named collection of evaluation cases."""

    model_config = ConfigDict(extra="forbid")

    name: str
    cases: list[EvalCase] = Field(default_factory=list)

    @classmethod
    def from_records(cls, name: str, records: list[dict[str, Any]]) -> EvalDataset:
        """Build a dataset from a list of plain dict records."""
        return cls(name=name, cases=[EvalCase.model_validate(record) for record in records])

    @classmethod
    def from_jsonl(cls, name: str, text: str) -> EvalDataset:
        """Build a dataset from JSONL text (one JSON object per non-blank line)."""
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        return cls.from_records(name, records)


class Score(BaseModel):
    """The outcome of one scorer on one case.

    ``error`` is set only when the scorer itself failed (e.g. an LLM judge raised
    or returned unparseable content) — this is distinct from a quality failure
    (``passed=False`` with no error).
    """

    model_config = ConfigDict(extra="forbid")

    scorer: str
    value: float = 0.0
    passed: bool = False
    rationale: str = ""
    error: str | None = None

    @property
    def errored(self) -> bool:
        """True when the scorer failed to produce a verdict."""
        return self.error is not None


class CaseResult(BaseModel):
    """The result of evaluating one case: the actual output and its scores."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    case_id: str
    output: Any = None
    scores: list[Score] = Field(default_factory=list)
    # Set when the target failed to produce any output for this case.
    error: str | None = None

    @property
    def errored(self) -> bool:
        """True when the target failed or any scorer errored."""
        return self.error is not None or any(score.errored for score in self.scores)

    @property
    def passed(self) -> bool:
        """True only when output was produced, no scorer errored, and all scorers passed."""
        return (
            self.error is None
            and bool(self.scores)
            and all(score.passed and not score.errored for score in self.scores)
        )


class EvalReport(BaseModel):
    """Aggregate results of an evaluation run.

    Errored cases (target or scorer failures) count as *not passed* in
    ``pass_rate`` and are surfaced separately via ``errored_count`` /
    ``error_rate``, so a 90% pass rate with a 30% error rate is visible rather
    than hidden.
    """

    model_config = ConfigDict(extra="forbid")

    dataset: str
    results: list[CaseResult] = Field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of cases evaluated."""
        return len(self.results)

    @property
    def passed_count(self) -> int:
        """Number of cases that passed all scorers with no errors."""
        return sum(1 for result in self.results if result.passed)

    @property
    def errored_count(self) -> int:
        """Number of cases with a target failure or a scorer error."""
        return sum(1 for result in self.results if result.errored)

    @property
    def pass_rate(self) -> float:
        """Fraction of all cases that passed (errored cases count against it)."""
        return self.passed_count / self.total if self.total else 0.0

    @property
    def error_rate(self) -> float:
        """Fraction of all cases that errored (harness/target failures)."""
        return self.errored_count / self.total if self.total else 0.0

    def mean_scores(self) -> dict[str, float]:
        """Mean value per scorer, computed over non-errored scores only."""
        buckets: dict[str, list[float]] = {}
        for result in self.results:
            for score in result.scores:
                if not score.errored:
                    buckets.setdefault(score.scorer, []).append(score.value)
        return {scorer: sum(values) / len(values) for scorer, values in buckets.items()}

    def summary(self) -> dict[str, Any]:
        """Return a JSON-friendly summary of the run's aggregate metrics."""
        return {
            "dataset": self.dataset,
            "total": self.total,
            "passed": self.passed_count,
            "errored": self.errored_count,
            "pass_rate": self.pass_rate,
            "error_rate": self.error_rate,
            "mean_scores": self.mean_scores(),
        }
