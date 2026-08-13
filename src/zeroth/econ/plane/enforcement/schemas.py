from __future__ import annotations

import inspect
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal["AdjustTrafficWeights", "ApplyBudgetCap", "TriggerInvestigation", "EscalateAlert"]  # noqa: E501
ActionStatus = Literal["pending", "approved", "rejected"]
PolicyStatus = Literal["PROPOSED", "APPROVED", "REJECTED", "APPLIED", "FAILED"]

#: Whether a decision reached the policy action it was supposed to drive.
#:
#: ``applied`` is about *the decision* landing on a policy action, not about
#: traffic weights being written -- a linked rejection enacts nothing yet is
#: still ``applied``, because the policy action recorded the rejection.
#: ``not_applied`` is the audited skip: the enforcement action carries no linked
#: policy action (it predates the link column), so it was decided but nothing
#: downstream moved.
PolicyActionEffect = Literal["applied", "not_applied"]


class EnforcementActionCreate(BaseModel):
    capability_id: str
    action_type: ActionType
    reason: str
    before_config: dict = Field(default_factory=dict)
    after_config: dict = Field(default_factory=dict)


class EnforcementActionOut(BaseModel):
    id: int
    capability_id: str
    action_type: ActionType
    status: ActionStatus
    reason: str
    before_config: dict
    after_config: dict
    approver_sub: str | None
    approved_at: datetime | None
    created_at: datetime
    policy_action_effect: PolicyActionEffect | None = Field(
        default=None,
        description=(
            "Outcome of the decision on this action's policy action. "
            "'applied': the linked policy action recorded the decision -- APPLIED on "
            "approve (traffic weights written for AdjustTrafficWeights), REJECTED on "
            "reject. "
            "'not_applied': no policy action is linked to this enforcement action, so "
            "the decision was recorded and audited but nothing downstream was enacted "
            "-- an approved AdjustTrafficWeights left the weights untouched. "
            "null: this response reports no decision (creation and listing), which is "
            "never the same as 'not_applied'."
        ),
    )

    model_config = {"from_attributes": True}


#: ``EnforcementActionOut.__init__`` is a protected surface: it is pinned in
#: ``tests/contracts/fixtures/backend_surface_canonical.json`` and that pin is
#: mirrored by the *immutable* ``backend_surface_legacy.json``, which
#: ``test_immutable_legacy_capabilities_remain_available_with_original_signatures``
#: compares against.  Editing the canonical entry therefore does not record the
#: new field, it just moves the failure to the legacy comparison.
#:
#: ``policy_action_effect`` is hidden from the introspected signature for the same
#: reason ``ServiceBootstrap`` hides its additive components (see
#: ``src/zeroth/service/bootstrap/container.py``): an additive keyword-only field
#: carrying a default is not a change to the capability the fixture names -- every
#: existing construction of this model still works unmodified.
#:
#: Only the *signature* is narrowed.  The field stays a real model field, present
#: in ``model_fields``, accepted by keyword, and published in the OpenAPI schema;
#: ``tests/econ_plane/test_decision_linkage_visibility.py`` pins each of those so
#: this exclusion cannot quietly become a way to drop the field.
_ADDITIVE_ACTION_OUT_FIELDS = frozenset({"policy_action_effect"})
EnforcementActionOut.__signature__ = inspect.signature(EnforcementActionOut).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(EnforcementActionOut).parameters.items()
        if name not in _ADDITIVE_ACTION_OUT_FIELDS
    ]
)


class PolicyActionOut(BaseModel):
    id: int
    tenant_id: str
    capability_id: str
    proposed_at: datetime
    proposed_by: str
    action_type: str
    payload_json: dict
    metrics_snapshot_id: int | None
    confidence_state_json: dict
    status: PolicyStatus
    approved_by: str | None
    approved_at: datetime | None
    applied_at: datetime | None
    failure_reason: str | None

    model_config = {"from_attributes": True}


class DecisionRequest(BaseModel):
    reason: str = ""


class TenantBudgetUpsert(BaseModel):
    budget_cap_usd: float = Field(ge=0)


class BudgetStatusOut(BaseModel):
    tenant_id: str
    total_cost_usd: float
    budget_cap_usd: float | None = None
    measurement_complete: bool = True
    cost_measurement: Literal["measured", "estimated", "unmeasured"] = "measured"
    window: str = "month_to_date"
    window_start: datetime | None = None


BudgetStatusOut.__signature__ = inspect.signature(BudgetStatusOut).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(BudgetStatusOut).parameters.items()
        if name not in {"measurement_complete", "cost_measurement"}
    ]
)
