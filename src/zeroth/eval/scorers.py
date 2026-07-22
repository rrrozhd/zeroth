"""Scorers for the evaluation harness (EVAL-02, EVAL-03).

A scorer grades one actual output against an :class:`EvalCase`, returning a
:class:`Score`. Deterministic scorers (exact-match, contains, regex, Pydantic-schema,
arbitrary predicate) are pure and fast; :class:`LLMJudgeScorer` uses a provider
adapter to grade open-ended output against a rubric.

These measure output *quality* — distinct from ``agent_runtime.OutputValidator``,
which validates output *shape* (EVAL-05). :class:`SchemaScorer` is the one
shape-as-quality check, offered as one scorer among many.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zeroth.eval.models import EvalCase, Score
from zeroth.runtime.agents.provider import ProviderAdapter, ProviderRequest, ProviderResponse


def _extract(output: Any, field: str | None) -> Any:
    """Return ``output[field]`` (for a mapping) or ``output`` when no field is given."""
    if field is None:
        return output
    if isinstance(output, Mapping):
        return output.get(field)
    return None


class Scorer(Protocol):
    """Grades one actual output against a case, returning a Score."""

    name: str

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Return a Score for ``output`` given the reference ``case``."""
        ...


class ExactMatchScorer:
    """Passes when the output (or a field of it) equals the case's expected value."""

    def __init__(self, *, field: str | None = None, name: str = "exact_match") -> None:
        self.name = name
        self._field = field

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Compare the extracted output value to ``case.expected`` for equality."""
        passed = _extract(output, self._field) == case.expected
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed)


class ContainsScorer:
    """Passes when the expected text is a substring of the output text."""

    def __init__(
        self, *, field: str | None = None, case_sensitive: bool = False, name: str = "contains"
    ) -> None:
        self.name = name
        self._field = field
        self._case_sensitive = case_sensitive

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Check whether ``str(case.expected)`` appears in the output text."""
        haystack = str(_extract(output, self._field) or "")
        needle = str(case.expected or "")
        if not self._case_sensitive:
            haystack, needle = haystack.lower(), needle.lower()
        passed = needle in haystack
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed)


class RegexScorer:
    """Passes when a regex matches the output text."""

    def __init__(self, pattern: str, *, field: str | None = None, name: str = "regex") -> None:
        self.name = name
        self._field = field
        self._pattern = re.compile(pattern)

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Search the output text for the configured pattern."""
        passed = self._pattern.search(str(_extract(output, self._field) or "")) is not None
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed)


class SchemaScorer:
    """Passes when the output validates against a Pydantic model.

    Uses the project's schema mechanism (Pydantic models), not raw JSON-Schema. A
    validation failure is a quality *fail* (``passed=False``), not a harness error.
    """

    def __init__(
        self, model: type[BaseModel], *, field: str | None = None, name: str = "schema"
    ) -> None:
        self.name = name
        self._model = model
        self._field = field

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Validate the extracted output against the Pydantic model."""
        try:
            self._model.model_validate(_extract(output, self._field))
        except ValidationError as exc:
            return Score(scorer=self.name, value=0.0, passed=False, rationale=str(exc))
        return Score(scorer=self.name, value=1.0, passed=True)


class PredicateScorer:
    """Passes when a user-supplied predicate ``fn(output, case)`` returns True."""

    def __init__(self, fn: Callable[[Any, EvalCase], bool], *, name: str = "predicate") -> None:
        self.name = name
        self._fn = fn

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Run the predicate, recording an error (not a fail) if it raises."""
        try:
            passed = bool(self._fn(output, case))
        except Exception as exc:  # predicate is user code; a crash is a harness error
            return Score(scorer=self.name, passed=False, error=f"predicate raised: {exc}")
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed)


class JudgeVerdict(BaseModel):
    """Structured verdict returned by an LLM judge."""

    model_config = ConfigDict(extra="ignore")

    score: float = Field(ge=0.0, le=1.0)
    # Required (no default): OpenAI strict structured output rejects schemas where
    # any property is omitted from `required`. A judge should always give a reason.
    rationale: str


_JUDGE_INSTRUCTION = (
    "You are grading an AI system's output. Apply the rubric and respond ONLY with "
    'JSON of the form {{"score": <float 0..1>, "rationale": "<short reason>"}}.\n\n'
    "Rubric:\n{rubric}\n\nInput:\n{input}\n\nExpected (reference, may be empty):\n"
    "{expected}\n\nActual output:\n{actual}"
)


class LLMJudgeScorer:
    """Grades open-ended output against a rubric using a provider adapter (EVAL-02).

    Requests structured output (:class:`JudgeVerdict`) for robustness. A provider
    failure or an unparseable verdict yields an *errored* Score (never a silent
    zero), so a flaky judge cannot read as a quality regression. ``passed`` is
    derived from the configurable ``pass_threshold`` rather than a model-emitted
    boolean.

    For a stable CI gate, run the judge at temperature 0, or use deterministic
    scorers as the gate and judges as advisory.
    """

    def __init__(
        self,
        provider: ProviderAdapter,
        model_name: str,
        *,
        rubric: str,
        pass_threshold: float = 0.7,
        name: str = "llm_judge",
    ) -> None:
        self.name = name
        self._provider = provider
        self._model_name = model_name
        self._rubric = rubric
        self._pass_threshold = pass_threshold

    async def score(self, output: Any, case: EvalCase) -> Score:
        """Ask the judge to grade ``output``; returns an errored Score on any failure."""
        rubric = case.rubric or self._rubric
        prompt = _JUDGE_INSTRUCTION.format(
            rubric=rubric,
            input=json.dumps(case.input, default=str),
            expected=json.dumps(case.expected, default=str),
            actual=json.dumps(output, default=str),
        )
        request = ProviderRequest(
            model_name=self._model_name,
            messages=[{"role": "user", "content": prompt}],
            output_model=JudgeVerdict,
        )
        try:
            response = await self._provider.ainvoke(request)
            verdict = self._extract_verdict(response)
        except Exception as exc:
            return Score(scorer=self.name, passed=False, error=f"judge failed: {exc}")
        passed = verdict.score >= self._pass_threshold
        return Score(
            scorer=self.name, value=verdict.score, passed=passed, rationale=verdict.rationale
        )

    @staticmethod
    def _extract_verdict(response: ProviderResponse) -> JudgeVerdict:
        """Coerce a provider response into a JudgeVerdict (raises if unparseable)."""
        content = response.content
        if isinstance(content, JudgeVerdict):
            return content
        if isinstance(content, Mapping):
            return JudgeVerdict.model_validate(content)
        if isinstance(content, str):
            text = content.strip()
            try:
                return JudgeVerdict.model_validate_json(text)
            except ValidationError:
                start, end = text.find("{"), text.rfind("}")
                if start != -1 and end > start:
                    return JudgeVerdict.model_validate_json(text[start : end + 1])
                raise
        raise ValueError(f"judge returned unparseable content of type {type(content).__name__}")
