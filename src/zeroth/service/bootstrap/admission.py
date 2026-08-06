"""The service-side implementation of governance's admission seam (ZER-24).

This is the other half of the E1 inversion. Governance owns
:class:`~zeroth.governance.decisions.admission.AdmissionEvaluator`; the service
domain owns the admission combiner that actually classifies, applies policy and
then applies budget. Binding the two here means the dependency runs
service → governance, which the policy permits, instead of governance → service,
which it does not.

It lives under ``service.bootstrap`` rather than beside ``admit`` because it is
wiring: it holds no admission logic of its own, only the two collaborators the
combiner needs. Keeping it here also keeps it out of the legacy gateway package
that ZER-24 is emptying, so no later relocation step has to move it again.
"""

from __future__ import annotations

from zeroth.contracts.langgraph_gateway.models import AdmissionDecision, AdmissionRequest
from zeroth.service.langgraph_gateway.admission import (
    BudgetChecker,
    InputClassifier,
    PolicyAdmissionChecker,
    admit,
)

__all__ = ["BoundAdmissionEvaluator"]


class BoundAdmissionEvaluator:
    """Bind a policy guard and a budget checker to the shared ``admit`` combiner.

    Satisfies :class:`~zeroth.governance.decisions.admission.AdmissionEvaluator`
    structurally rather than by inheritance, so governance stays free of any
    import back into this module.
    """

    def __init__(
        self,
        *,
        policy_guard: PolicyAdmissionChecker,
        budget_checker: BudgetChecker,
        classifier: InputClassifier | None = None,
    ) -> None:
        """Wire the evaluator.

        Args:
            policy_guard: Evaluates run admission constraints.
            budget_checker: Reports the tenant's budget posture.
            classifier: Optional input classifier. Left ``None``, ``admit``
                falls back to its own conservative default, which classifies
                everything as ``unclassified`` rather than guessing.
        """
        self._policy_guard = policy_guard
        self._budget_checker = budget_checker
        self._classifier = classifier

    async def evaluate(self, request: AdmissionRequest) -> AdmissionDecision:
        """Rule on ``request`` using the shared admission combiner.

        Args:
            request: The classified admission request to rule on.

        Returns:
            The combiner's decision, passed through unaltered. Translating it
            here would put a second policy in the path; the caller maps
            refusals onto its own audit vocabulary.
        """
        return await admit(
            request,
            policy_guard=self._policy_guard,
            budget_checker=self._budget_checker,
            classifier=self._classifier,
        )
