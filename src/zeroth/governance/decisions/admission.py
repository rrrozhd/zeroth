"""The admission seam :class:`ToolDecisionService` decides against.

Governance owns this protocol so that depending on it is a legal edge. Before
ZER-24 the decision service imported the admission orchestrator out of the
service-classified ``langgraph_gateway`` package directly, which is the
dependency temporary exception E1 existed to permit: governance may depend only
on contracts and platform.

Inverting it moves the dependency rather than duplicating the logic. The
service domain still owns the combiner that classifies, applies policy and then
applies budget; it now supplies that combiner *to* governance as an
:class:`AdmissionEvaluator` instead of governance reaching into it. There is
still exactly one admission path, and no second combiner to drift.

The request and decision types are contracts, so naming them here costs
governance nothing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zeroth.contracts.langgraph_gateway.models import AdmissionDecision, AdmissionRequest

__all__ = ["AdmissionEvaluator"]


@runtime_checkable
class AdmissionEvaluator(Protocol):
    """Rule on whether one classified request may be admitted.

    Implementations are injected, so the caller controls neither their failure
    modes nor their return type. A caller must therefore treat a raise and a
    non-:class:`AdmissionDecision` result as refusals rather than trusting the
    annotation below -- see
    :meth:`~zeroth.governance.decisions.service.ToolDecisionService._admit`.
    """

    async def evaluate(self, request: AdmissionRequest) -> AdmissionDecision:
        """Rule on ``request``.

        Args:
            request: The classified admission request to rule on.

        Returns:
            The admission decision. ``allowed=True`` carrying
            ``budget_check_degraded=True`` is a fail-*open* outage answer, not
            a justification, and callers that exist to refuse what they cannot
            justify are expected to reject it.
        """
        ...
