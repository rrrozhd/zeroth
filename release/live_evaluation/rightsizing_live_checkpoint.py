"""Provider-gated live evidence harness for the two remaining rightsizing criteria.

The module deliberately separates three boundaries:

* :meth:`RightsizingLiveHarness.readiness` is provider-free and only checks whether the
  logical ``llm.openai`` reference is registered.
* :meth:`RightsizingLiveHarness.run` requires an exact, explicit arm phrase and gives an
  injected executor the logical reference, never a credential value.
* :func:`seal_capture` accepts only already-sanitized observations, rechecks economics and
  quality persistence, and seals immutable evidence with checksums.

No import or dry-run path performs network I/O or resolves secret material.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from .evidence import AcceptanceCriterion, EvidenceStore
from .reconciliation import ProviderWindowSummary, ReconciliationInput, reconcile_campaign

MODEL = "openai/gpt-4o-mini"
CREDENTIAL_REFERENCE = "llm.openai"
ARM_PHRASE = "AUTHORIZE_RIGHTSIZING_PROVIDER_SPEND"
PER_RUN_CAP_USD = Decimal("0.25")
CAMPAIGN_CAP_USD = Decimal("10.00")
TOLERANCE_FLOOR_USD = Decimal("0.000001")
TOLERANCE_RATE = Decimal("0.005")
DEFAULT_CASES = Path(__file__).with_name("rightsizing-recorded-cases-v1.json")

Role = Literal["incumbent", "candidate", "judge"]
_ROLES: tuple[Role, ...] = ("incumbent", "candidate", "judge")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: str, field: str) -> None:
    if not value or len(value) > 512 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(f"{field} must be a nonblank opaque identifier")


def _money(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative Decimal")


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _outside_tolerance(left: Decimal, right: Decimal) -> bool:
    tolerance = max(TOLERANCE_FLOOR_USD, max(abs(left), abs(right)) * TOLERANCE_RATE)
    return abs(left - right) > tolerance


@dataclass(frozen=True, slots=True)
class RecordedAgentCase:
    case_id: str
    input: Mapping[str, object]
    reference: str

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if not self.input or not self.reference.strip():
            raise ValueError("recorded cases require nonempty input and reference output")


@dataclass(frozen=True, slots=True)
class UsageObservation:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise ValueError("token usage cannot be negative")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total token usage must equal input plus output")


@dataclass(frozen=True, slots=True)
class CallObservation:
    case_id: str
    role: Role
    model: str
    provider_request_id: str
    usage: UsageObservation
    measured_cost_usd: Decimal
    estimated_cost_usd: Decimal
    cost_event_id: str
    audit_event_id: str
    operation_id: str
    run_id: str

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        if self.role not in _ROLES:
            raise ValueError("unsupported rightsizing call role")
        if self.model != MODEL:
            raise ValueError(f"live rightsizing calls must use {MODEL}")
        for field in (
            "provider_request_id",
            "cost_event_id",
            "audit_event_id",
            "operation_id",
            "run_id",
        ):
            _identifier(getattr(self, field), field)
        _money(self.measured_cost_usd, "measured_cost_usd")
        _money(self.estimated_cost_usd, "estimated_cost_usd")


@dataclass(frozen=True, slots=True)
class QualityVerdictObservation:
    case_id: str
    candidate_request_id: str
    judge_request_id: str
    score: Decimal
    passed: bool
    persistence_id: str
    persisted: bool
    readback_verified: bool

    def __post_init__(self) -> None:
        for field in ("case_id", "candidate_request_id", "judge_request_id", "persistence_id"):
            _identifier(getattr(self, field), field)
        if not self.score.is_finite() or not Decimal("0") <= self.score <= Decimal("1"):
            raise ValueError("quality score must be between zero and one")


@dataclass(frozen=True, slots=True)
class RightsizingCapture:
    campaign_id: str
    cases_sha256: str
    calls: tuple[CallObservation, ...]
    verdicts: tuple[QualityVerdictObservation, ...]
    reconciliation: ReconciliationInput
    prior_campaign_spend_usd: Decimal

    def __post_init__(self) -> None:
        _identifier(self.campaign_id, "campaign_id")
        if not _SHA256.fullmatch(self.cases_sha256):
            raise ValueError("cases_sha256 must be a lowercase SHA-256 digest")
        _money(self.prior_campaign_spend_usd, "prior_campaign_spend_usd")


@dataclass(frozen=True, slots=True)
class ServiceCallObservation:
    """One call reported by the real measured-experiment endpoint.

    The endpoint does not expose replay/judge roles, so this contract preserves
    only identities the product actually emitted. Assigning roles from call
    order would manufacture evidence.
    """

    operation_id: str
    provider_request_id: str | None
    cost_event_id: str
    audit_event_id: str
    model: str
    cost_measurement: str
    measured_cost_usd: Decimal | None
    estimated_cost_usd: Decimal | None
    input_tokens: int
    output_tokens: int
    cleanup_status: str

    def __post_init__(self) -> None:
        for field in (
            "operation_id",
            "cost_event_id",
            "audit_event_id",
            "model",
            "cost_measurement",
            "cleanup_status",
        ):
            _identifier(getattr(self, field), field)
        if self.provider_request_id is not None:
            _identifier(self.provider_request_id, "provider_request_id")
        if self.cost_measurement not in {"measured", "estimated"}:
            raise ValueError("cost_measurement must be measured or estimated")
        if self.measured_cost_usd is not None:
            _money(self.measured_cost_usd, "measured_cost_usd")
        if self.estimated_cost_usd is not None:
            _money(self.estimated_cost_usd, "estimated_cost_usd")
        if self.measured_cost_usd is None and self.estimated_cost_usd is None:
            raise ValueError("a live provider call requires measured or estimated cost")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("provider token usage cannot be negative")
        if self.cleanup_status != "complete":
            raise ValueError("live provider call cleanup must be complete")


@dataclass(frozen=True, slots=True)
class ServiceOutcomeObservation:
    model: str
    provider: str
    is_incumbent: bool
    cases_evaluated: int
    cases_errored: int
    meets_bar: bool

    def __post_init__(self) -> None:
        _identifier(self.model, "outcome model")
        _identifier(self.provider, "outcome provider")
        if self.cases_evaluated < 0 or not 0 <= self.cases_errored <= self.cases_evaluated:
            raise ValueError("outcome case counts are invalid")


@dataclass(frozen=True, slots=True)
class ServiceRightsizingCapture:
    """Sanitized response and durable readbacks from the actual service route."""

    campaign_id: str
    cases_sha256: str
    run_id: str
    node_id: str
    mode: Literal["equivalence", "correctness"]
    cases: int
    min_cases: int
    verdict: Literal["confirmed", "flagged", "none"]
    recommended_model: str | None
    calls: tuple[ServiceCallObservation, ...]
    outcomes: tuple[ServiceOutcomeObservation, ...]
    response_measured_cost_usd: Decimal
    response_estimated_cost_usd: Decimal
    reconciliation: ReconciliationInput
    prior_campaign_spend_usd: Decimal

    def __post_init__(self) -> None:
        for value, field in (
            (self.campaign_id, "campaign_id"),
            (self.run_id, "run_id"),
            (self.node_id, "node_id"),
        ):
            _identifier(value, field)
        if not _SHA256.fullmatch(self.cases_sha256):
            raise ValueError("cases_sha256 must be a lowercase SHA-256 digest")
        if self.cases <= 0 or self.min_cases <= 0:
            raise ValueError("measured service experiment requires positive case counts")
        _money(self.response_measured_cost_usd, "response_measured_cost_usd")
        _money(self.response_estimated_cost_usd, "response_estimated_cost_usd")
        _money(self.prior_campaign_spend_usd, "prior_campaign_spend_usd")


@dataclass(frozen=True, slots=True)
class ReadinessPlan:
    ready: bool
    cases: int
    cases_sha256: str
    provider_calls_performed: Literal[0] = 0
    credential_reference: str = CREDENTIAL_REFERENCE
    model: str = MODEL
    per_run_cap_usd: Decimal = PER_RUN_CAP_USD
    campaign_cap_usd: Decimal = CAMPAIGN_CAP_USD
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PaidAuthorization:
    credential_reference: Literal["llm.openai"] = CREDENTIAL_REFERENCE
    model: Literal["openai/gpt-4o-mini"] = MODEL
    per_run_cap_usd: Decimal = PER_RUN_CAP_USD
    campaign_cap_usd: Decimal = CAMPAIGN_CAP_USD


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    measured_total_usd: Decimal
    estimated_total_usd: Decimal
    quality_pass_rate: Decimal
    provider_window_policy: Literal[
        "upper_bound_only", "unavailable_campaign_local_only"
    ] = "upper_bound_only"


def _provider_window_policy(
    provider_window: ProviderWindowSummary,
    accounted_total: Decimal,
) -> Literal["upper_bound_only", "unavailable_campaign_local_only"]:
    if provider_window.window_id.startswith("unavailable:"):
        if provider_window.total_usd != 0:
            raise ValueError("unavailable provider-project window must not invent usage")
        return "unavailable_campaign_local_only"
    provider_total = provider_window.total_usd
    tolerance = max(
        TOLERANCE_FLOOR_USD,
        max(provider_total, accounted_total) * TOLERANCE_RATE,
    )
    if provider_total + tolerance < accounted_total:
        raise ValueError("provider-project window is below tagged campaign spend")
    return "upper_bound_only"


def _decimal_field(value: object, field: str, *, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a decimal amount")
    try:
        rendered = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a decimal amount") from exc
    _money(rendered, field)
    return rendered


def _required_identifier(row: Mapping[str, object], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a nonblank opaque identifier")
    _identifier(value, field)
    return value


def _optional_identifier(row: Mapping[str, object], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an opaque identifier when available")
    _identifier(value, field)
    return value


class SecretReferenceCatalog(Protocol):
    """Presence-only secret catalog; it cannot return credential values."""

    def has_reference(self, logical_name: str) -> bool: ...


class LiveExecutor(Protocol):
    """Externally owned product executor for one sanitized provider observation."""

    def execute(
        self,
        *,
        case: RecordedAgentCase,
        role: Role,
        authorization: PaidAuthorization,
    ) -> CallObservation: ...


def load_recorded_cases(source: Path = DEFAULT_CASES) -> tuple[tuple[RecordedAgentCase, ...], str]:
    """Load a deterministic, replayable JSON case set and return its byte checksum."""
    payload = source.expanduser().resolve(strict=True).read_bytes()
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("recorded rightsizing cases are malformed JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("recorded rightsizing cases require schema_version 1")
    rows = raw.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("recorded rightsizing cases must be a nonempty list")
    cases: list[RecordedAgentCase] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"case_id", "input", "reference"}:
            raise ValueError("recorded rightsizing case has an invalid field contract")
        if not isinstance(row["input"], dict) or not isinstance(row["reference"], str):
            raise ValueError("recorded rightsizing case has invalid input or reference")
        cases.append(RecordedAgentCase(**row))
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("recorded rightsizing case IDs must be unique")
    return tuple(cases), hashlib.sha256(payload).hexdigest()


class RightsizingLiveHarness:
    """Default-deny coordinator; provider behavior exists only behind an injected executor."""

    def __init__(self, *, secret_catalog: SecretReferenceCatalog) -> None:
        self._secret_catalog = secret_catalog

    def readiness(self, cases_file: Path = DEFAULT_CASES) -> ReadinessPlan:
        cases, digest = load_recorded_cases(cases_file)
        present = self._secret_catalog.has_reference(CREDENTIAL_REFERENCE)
        return ReadinessPlan(
            ready=present,
            cases=len(cases),
            cases_sha256=digest,
            blockers=(
                ()
                if present
                else ("logical provider credential reference llm.openai is unavailable",)
            ),
        )

    def run(
        self,
        *,
        cases_file: Path,
        executor: LiveExecutor,
        arm: str,
        prior_campaign_spend_usd: Decimal,
    ) -> tuple[CallObservation, ...]:
        plan = self.readiness(cases_file)
        if arm != ARM_PHRASE:
            raise PermissionError("live provider execution is not explicitly armed")
        if not plan.ready:
            raise PermissionError(plan.blockers[0])
        _money(prior_campaign_spend_usd, "prior_campaign_spend_usd")
        worst_case_reservation = Decimal(plan.cases * len(_ROLES)) * PER_RUN_CAP_USD
        if prior_campaign_spend_usd + worst_case_reservation > CAMPAIGN_CAP_USD:
            raise PermissionError(
                "remaining campaign capacity cannot reserve every planned provider call"
            )
        cases, digest = load_recorded_cases(cases_file)
        if digest != plan.cases_sha256:
            raise RuntimeError("recorded case file changed after readiness check")
        authorization = PaidAuthorization()
        observations: list[CallObservation] = []
        spend_by_case: defaultdict[str, Decimal] = defaultdict(Decimal)
        campaign_spend = prior_campaign_spend_usd
        for case in cases:
            for role in _ROLES:
                observation = executor.execute(
                    case=case,
                    role=role,
                    authorization=authorization,
                )
                if (observation.case_id, observation.role) != (case.case_id, role):
                    raise RuntimeError("executor returned an observation for a different call")
                conservative_cost = max(
                    observation.measured_cost_usd, observation.estimated_cost_usd
                )
                spend_by_case[case.case_id] += conservative_cost
                campaign_spend += conservative_cost
                if spend_by_case[case.case_id] > PER_RUN_CAP_USD:
                    raise RuntimeError("rightsizing case exceeded the $0.25 run cap")
                if campaign_spend > CAMPAIGN_CAP_USD:
                    raise RuntimeError("rightsizing campaign exceeded the $10 campaign cap")
                observations.append(observation)
        return tuple(observations)


def capture_service_response(
    *,
    response: Mapping[str, object],
    cases_sha256: str,
    prior_campaign_spend_usd: Decimal,
    reconciliation: ReconciliationInput,
) -> ServiceRightsizingCapture:
    """Parse the actual ``/econ/rightsizing/experiment`` JSON contract.

    This is intentionally not a role-oriented adapter. The public route reports
    call identities and models but not internal replay/judge roles; acceptance
    retains exactly that product truth and fails closed if any live-call identity
    is missing.
    """
    execution_raw = response.get("execution")
    if not isinstance(execution_raw, Mapping):
        raise ValueError("measured service response is missing execution evidence")
    calls_raw = execution_raw.get("calls")
    if not isinstance(calls_raw, list) or not calls_raw:
        raise ValueError("measured service response requires live provider calls")
    calls: list[ServiceCallObservation] = []
    for raw in calls_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("service call evidence must be an object")
        if raw.get("provider_call_attempted") is not True or raw.get("cache_hit") is not False:
            raise ValueError("measured checkpoint requires a non-cache live provider call")
        input_tokens = raw.get("input_tokens")
        output_tokens = raw.get("output_tokens")
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
            raise ValueError("input_tokens must be a nonnegative integer")
        if isinstance(output_tokens, bool) or not isinstance(output_tokens, int):
            raise ValueError("output_tokens must be a nonnegative integer")
        calls.append(
            ServiceCallObservation(
                operation_id=_required_identifier(raw, "operation_id"),
                provider_request_id=_optional_identifier(raw, "provider_request_id"),
                cost_event_id=_required_identifier(raw, "cost_event_id"),
                audit_event_id=_required_identifier(raw, "audit_event_id"),
                model=_required_identifier(raw, "model"),
                cost_measurement=_required_identifier(raw, "cost_measurement"),
                measured_cost_usd=_decimal_field(
                    raw.get("measured_cost_usd"), "measured_cost_usd", nullable=True
                ),
                estimated_cost_usd=_decimal_field(
                    raw.get("estimated_cost_usd"), "estimated_cost_usd", nullable=True
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cleanup_status=_required_identifier(raw, "cleanup_status"),
            )
        )
    provider_call_count = execution_raw.get("provider_call_count")
    if (
        isinstance(provider_call_count, bool)
        or not isinstance(provider_call_count, int)
        or provider_call_count != len(calls)
    ):
        raise ValueError("provider_call_count does not match live non-cache calls")

    outcomes_raw = response.get("outcomes")
    if not isinstance(outcomes_raw, list) or not outcomes_raw:
        raise ValueError("measured service response requires candidate outcomes")
    outcomes: list[ServiceOutcomeObservation] = []
    for raw in outcomes_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("service outcome evidence must be an object")
        cases_evaluated = raw.get("cases_evaluated")
        cases_errored = raw.get("cases_errored")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (cases_evaluated, cases_errored)
        ):
            raise ValueError("outcome case counts must be integers")
        outcomes.append(
            ServiceOutcomeObservation(
                model=_required_identifier(raw, "model"),
                provider=_required_identifier(raw, "provider"),
                is_incumbent=raw.get("is_incumbent") is True,
                cases_evaluated=cases_evaluated,
                cases_errored=cases_errored,
                meets_bar=raw.get("meets_bar") is True,
            )
        )
    if not any(not outcome.is_incumbent for outcome in outcomes):
        raise ValueError("measured service response requires a candidate outcome")

    mode = response.get("mode")
    verdict = response.get("verdict")
    if mode not in {"equivalence", "correctness"}:
        raise ValueError("service experiment mode is invalid")
    if verdict not in {"confirmed", "flagged", "none"}:
        raise ValueError("service experiment verdict is invalid")
    cases = response.get("cases")
    min_cases = response.get("min_cases")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (cases, min_cases)):
        raise ValueError("service experiment case counts must be integers")
    recommended = response.get("recommended_model")
    if recommended is not None:
        if not isinstance(recommended, str):
            raise ValueError("recommended_model must be an opaque identifier")
        _identifier(recommended, "recommended_model")
    campaign_id = _required_identifier(execution_raw, "campaign_id")
    measured = _decimal_field(execution_raw.get("measured_cost_usd"), "measured_cost_usd")
    estimated = _decimal_field(execution_raw.get("estimated_cost_usd"), "estimated_cost_usd")
    assert measured is not None and estimated is not None
    if measured != sum((call.measured_cost_usd or Decimal("0") for call in calls), Decimal("0")):
        raise ValueError("service response measured cost total does not match its calls")
    if estimated != sum((call.estimated_cost_usd or Decimal("0") for call in calls), Decimal("0")):
        raise ValueError("service response estimated cost total does not match its calls")
    return ServiceRightsizingCapture(
        campaign_id=campaign_id,
        cases_sha256=cases_sha256,
        run_id=_required_identifier(execution_raw, "run_id"),
        node_id=_required_identifier(response, "node_id"),
        mode=mode,
        cases=cases,
        min_cases=min_cases,
        verdict=verdict,
        recommended_model=recommended,
        calls=tuple(calls),
        outcomes=tuple(outcomes),
        response_measured_cost_usd=measured,
        response_estimated_cost_usd=estimated,
        reconciliation=reconciliation,
        prior_campaign_spend_usd=prior_campaign_spend_usd,
    )


def _accounted_cost(call: CallObservation | ServiceCallObservation) -> Decimal:
    measured = call.measured_cost_usd
    if measured is not None:
        return measured
    estimated = call.estimated_cost_usd
    if estimated is None:
        raise ValueError("provider call has no accountable cost")
    return estimated


def _validate_reconciliation(
    calls: tuple[CallObservation, ...] | tuple[ServiceCallObservation, ...],
    reconciliation: ReconciliationInput,
    *,
    service_run_id: str | None = None,
) -> Decimal:
    calls_by_operation = {call.operation_id: call for call in calls}
    if len(calls_by_operation) != len(calls):
        raise ValueError("duplicate live call identity: operation_id")
    provider_request_ids = [
        call.provider_request_id for call in calls if call.provider_request_id is not None
    ]
    if len(set(provider_request_ids)) != len(provider_request_ids):
        raise ValueError("duplicate live call identity: provider_request_id")
    audit_by_operation = {item.operation_id: item for item in reconciliation.audits}
    local_by_operation = {item.operation_id: item for item in reconciliation.local_cost_events}
    regulus_by_operation = {item.operation_id: item for item in reconciliation.regulus_events}
    reservations_by_operation = {item.operation_id: item for item in reconciliation.reservations}
    expected_operations = set(calls_by_operation)
    if not (
        set(audit_by_operation)
        == set(local_by_operation)
        == set(regulus_by_operation)
        == expected_operations
    ):
        raise ValueError("reconciliation is missing or contains unexpected provider calls")
    for operation_id, call in calls_by_operation.items():
        audit = audit_by_operation[operation_id]
        local = local_by_operation[operation_id]
        regulus = regulus_by_operation[operation_id]
        expected_run_id = call.run_id if isinstance(call, CallObservation) else service_run_id
        if expected_run_id is None:
            raise ValueError("reconciliation service run identity is missing")
        if any(item.run_id != expected_run_id for item in (audit, local, regulus)):
            raise ValueError("reconciliation service run identity mismatch")
        expected_identity = (
            call.audit_event_id,
            call.operation_id,
            expected_run_id,
            call.cost_event_id,
        )
        if (
            (audit.audit_event_id, audit.operation_id, audit.run_id, audit.cost_event_id)
            != expected_identity
            or (local.audit_event_id, local.operation_id, local.run_id, local.cost_event_id)
            != expected_identity
            or (regulus.audit_event_id, regulus.operation_id, regulus.run_id, regulus.cost_event_id)
            != expected_identity
        ):
            raise ValueError("reconciliation identity mismatch")
        if any(
            item.provider_request_id != call.provider_request_id
            for item in (audit, local, regulus)
        ):
            raise ValueError("reconciliation provider request identity mismatch")
        if not audit.signed or not audit.chain_verified:
            raise ValueError("reconciliation audit chain is not signed and verified")
        accounted = _accounted_cost(call)
        if any(
            _outside_tolerance(accounted, amount)
            for amount in (audit.cost_usd, local.amount_usd, regulus.amount_usd)
        ):
            raise ValueError("reconciliation cost planes exceed max($0.000001, 0.5%)")
        reservation = reservations_by_operation.get(call.operation_id)
        if (
            reservation is None
            or reservation.state != "committed"
            or reservation.run_id != expected_run_id
            or reservation.maximum_usd > PER_RUN_CAP_USD
        ):
            raise ValueError("reconciliation reservation is absent, invalid, or over cap")
    accounted_total = sum((_accounted_cost(call) for call in calls), Decimal("0"))
    for total in (
        sum((item.cost_usd for item in reconciliation.audits), Decimal("0")),
        sum((item.amount_usd for item in reconciliation.local_cost_events), Decimal("0")),
        sum((item.amount_usd for item in reconciliation.regulus_events), Decimal("0")),
    ):
        if _outside_tolerance(accounted_total, total):
            raise ValueError("reconciliation totals exceed max($0.000001, 0.5%)")
    _provider_window_policy(reconciliation.provider_window, accounted_total)
    return accounted_total


def validate_service_capture(capture: ServiceRightsizingCapture) -> ValidationSummary:
    """Validate the real endpoint result without inferring internal call roles."""
    if not capture.calls:
        raise ValueError("measured service experiment requires provider calls")
    for field in ("cost_event_id", "audit_event_id", "operation_id"):
        values = [getattr(call, field) for call in capture.calls]
        if len(set(values)) != len(values):
            raise ValueError(f"duplicate live call identity: {field}")
    provider_request_ids = [
        call.provider_request_id
        for call in capture.calls
        if call.provider_request_id is not None
    ]
    if len(set(provider_request_ids)) != len(provider_request_ids):
        raise ValueError("duplicate live call identity: provider_request_id")
    conservative_total = sum(
        (
            max(call.measured_cost_usd or Decimal("0"), call.estimated_cost_usd or Decimal("0"))
            for call in capture.calls
        ),
        Decimal("0"),
    )
    if conservative_total > PER_RUN_CAP_USD:
        raise ValueError("measured service experiment exceeds the $0.25 per-run cap")
    if capture.prior_campaign_spend_usd + conservative_total > CAMPAIGN_CAP_USD:
        raise ValueError("measured service experiment exceeds the $10 campaign cap")
    _validate_reconciliation(
        capture.calls,
        capture.reconciliation,
        service_run_id=capture.run_id,
    )
    candidates = [outcome for outcome in capture.outcomes if not outcome.is_incumbent]
    passed = sum(outcome.meets_bar for outcome in candidates)
    return ValidationSummary(
        measured_total_usd=capture.response_measured_cost_usd,
        estimated_total_usd=capture.response_estimated_cost_usd,
        quality_pass_rate=Decimal(passed) / Decimal(len(candidates)),
        provider_window_policy=_provider_window_policy(
            capture.reconciliation.provider_window,
            capture.response_measured_cost_usd,
        ),
    )


def validate_capture(capture: RightsizingCapture) -> ValidationSummary:
    """Fail closed unless calls, persisted verdicts, and three cost planes agree."""
    if not capture.calls:
        raise ValueError("measured experiment requires provider calls")
    grouped: defaultdict[str, list[CallObservation]] = defaultdict(list)
    for call in capture.calls:
        grouped[call.case_id].append(call)
    for case_id, calls in grouped.items():
        if Counter(call.role for call in calls) != Counter(_ROLES):
            raise ValueError(f"case {case_id} requires one incumbent, candidate, and judge call")

    identity_fields = (
        "provider_request_id",
        "cost_event_id",
        "audit_event_id",
        "operation_id",
    )
    for field in identity_fields:
        values = [getattr(call, field) for call in capture.calls]
        if len(set(values)) != len(values):
            raise ValueError(f"duplicate live call identity: {field}")

    calls_by_request = {call.provider_request_id: call for call in capture.calls}
    verdicts_by_case = {verdict.case_id: verdict for verdict in capture.verdicts}
    if len(verdicts_by_case) != len(capture.verdicts) or set(verdicts_by_case) != set(grouped):
        raise ValueError("quality verdicts must map one-to-one to recorded cases")
    for case_id, verdict in verdicts_by_case.items():
        if not verdict.persisted or not verdict.readback_verified:
            raise ValueError("quality verdict must be persisted and read back")
        candidate = calls_by_request.get(verdict.candidate_request_id)
        judge = calls_by_request.get(verdict.judge_request_id)
        if (
            candidate is None
            or candidate.case_id != case_id
            or candidate.role != "candidate"
            or judge is None
            or judge.case_id != case_id
            or judge.role != "judge"
        ):
            raise ValueError("quality verdict request identities do not match candidate and judge")

    measured_total = sum((call.measured_cost_usd for call in capture.calls), Decimal("0"))
    estimated_total = sum((call.estimated_cost_usd for call in capture.calls), Decimal("0"))
    conservative_by_case = {
        case_id: sum(
            (max(call.measured_cost_usd, call.estimated_cost_usd) for call in calls),
            Decimal("0"),
        )
        for case_id, calls in grouped.items()
    }
    if any(total > PER_RUN_CAP_USD for total in conservative_by_case.values()):
        raise ValueError("measured experiment exceeds the $0.25 per-run cap")
    conservative_total = sum(conservative_by_case.values(), Decimal("0"))
    if capture.prior_campaign_spend_usd + conservative_total > CAMPAIGN_CAP_USD:
        raise ValueError("measured experiment exceeds the $10 campaign cap")

    reconciliation = capture.reconciliation
    audit_by_request = {item.provider_request_id: item for item in reconciliation.audits}
    local_by_request = {item.provider_request_id: item for item in reconciliation.local_cost_events}
    regulus_by_request = {item.provider_request_id: item for item in reconciliation.regulus_events}
    reservations_by_operation = {item.operation_id: item for item in reconciliation.reservations}
    expected_requests = set(calls_by_request)
    if not (
        set(audit_by_request)
        == set(local_by_request)
        == set(regulus_by_request)
        == expected_requests
    ):
        raise ValueError("reconciliation is missing or contains unexpected provider calls")
    for request_id, call in calls_by_request.items():
        audit = audit_by_request[request_id]
        local = local_by_request[request_id]
        regulus = regulus_by_request[request_id]
        expected_identity = (
            call.audit_event_id,
            call.operation_id,
            call.run_id,
            call.cost_event_id,
        )
        if (
            (audit.audit_event_id, audit.operation_id, audit.run_id, audit.cost_event_id)
            != expected_identity
            or (local.audit_event_id, local.operation_id, local.run_id, local.cost_event_id)
            != expected_identity
            or (regulus.audit_event_id, regulus.operation_id, regulus.run_id, regulus.cost_event_id)
            != expected_identity
        ):
            raise ValueError("reconciliation identity mismatch")
        if not audit.signed or not audit.chain_verified:
            raise ValueError("reconciliation audit chain is not signed and verified")
        if any(
            _outside_tolerance(call.measured_cost_usd, amount)
            for amount in (audit.cost_usd, local.amount_usd, regulus.amount_usd)
        ):
            raise ValueError("reconciliation cost planes exceed max($0.000001, 0.5%)")
        reservation = reservations_by_operation.get(call.operation_id)
        if (
            reservation is None
            or reservation.state != "committed"
            or reservation.run_id != call.run_id
            or reservation.maximum_usd > PER_RUN_CAP_USD
        ):
            raise ValueError("reconciliation reservation is absent, invalid, or over cap")

    for total in (
        sum((item.cost_usd for item in reconciliation.audits), Decimal("0")),
        sum((item.amount_usd for item in reconciliation.local_cost_events), Decimal("0")),
        sum((item.amount_usd for item in reconciliation.regulus_events), Decimal("0")),
    ):
        if _outside_tolerance(measured_total, total):
            raise ValueError("reconciliation totals exceed max($0.000001, 0.5%)")
    provider_window_policy = _provider_window_policy(
        reconciliation.provider_window,
        measured_total,
    )

    passed = sum(1 for verdict in capture.verdicts if verdict.passed)
    return ValidationSummary(
        measured_total_usd=measured_total,
        estimated_total_usd=estimated_total,
        quality_pass_rate=Decimal(passed) / Decimal(len(capture.verdicts)),
        provider_window_policy=provider_window_policy,
    )


def seal_capture(*, capture: RightsizingCapture, screenshot: Path, destination: Path) -> Path:
    """Seal exact rightsizing acceptance only after all live evidence validates."""
    summary = validate_capture(capture)
    store = EvidenceStore(destination)
    store.validate(_jsonable(asdict(capture)))
    reconciliation_result = reconcile_campaign(store, capture.reconciliation)
    if not reconciliation_result.passed:
        raise ValueError("generic campaign reconciliation rejected the rightsizing capture")

    runtime = {
        "campaign_id": capture.campaign_id,
        "cases_sha256": capture.cases_sha256,
        "model": MODEL,
        "roles": list(_ROLES),
        "calls": [_jsonable(asdict(call)) for call in capture.calls],
        "quality_verdicts": [_jsonable(asdict(verdict)) for verdict in capture.verdicts],
        "quality_pass_rate": format(summary.quality_pass_rate, "f"),
    }
    audit = {
        "signed_chain_verified": all(
            item.signed and item.chain_verified for item in capture.reconciliation.audits
        ),
        "records": [_jsonable(asdict(item)) for item in capture.reconciliation.audits],
    }
    economics = {
        "campaign_cap_usd": format(CAMPAIGN_CAP_USD, "f"),
        "per_run_cap_usd": format(PER_RUN_CAP_USD, "f"),
        "measured_total_usd": format(summary.measured_total_usd, "f"),
        "estimated_total_usd": format(summary.estimated_total_usd, "f"),
        "provider_window_policy": summary.provider_window_policy,
        "provider_window": _jsonable(asdict(capture.reconciliation.provider_window)),
        "local_cost_events": [
            _jsonable(asdict(item)) for item in capture.reconciliation.local_cost_events
        ],
        "regulus_events": [
            _jsonable(asdict(item)) for item in capture.reconciliation.regulus_events
        ],
        "tolerance": "max(0.000001 USD, 0.5 percent)",
    }
    store._write_exclusive(Path("reconciliation/runtime.json"), runtime)
    store._write_exclusive(Path("reconciliation/audit.json"), audit)
    store._write_exclusive(Path("reconciliation/economics.json"), economics)
    store.ingest_artifact(screenshot, "screenshots/rightsizing-live.png")
    event_id = store.append_event(
        "rightsizing.experiment.verified",
        {
            "campaign_id": capture.campaign_id,
            "case_count": len(capture.verdicts),
            "call_count": len(capture.calls),
            "quality_pass_rate": format(summary.quality_pass_rate, "f"),
            "measured_total_usd": format(summary.measured_total_usd, "f"),
            "provider_window_policy": summary.provider_window_policy,
        },
    )
    evidence = (
        "screenshots/rightsizing-live.png",
        "reconciliation/runtime.json",
        "reconciliation/audit.json",
        "reconciliation/economics.json",
        f"events.ndjson#{event_id}",
    )
    acceptance = (
        AcceptanceCriterion("rightsizing.measured-experiment", "pass", evidence),
        AcceptanceCriterion("rightsizing.cost-reconciliation", "pass", evidence),
    )
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# Measured rightsizing live checkpoint\n\n"
            f"{len(capture.verdicts)} recorded cases ran through incumbent, candidate, and "
            f"judge calls on `{MODEL}`. Candidate quality verdicts were persisted and read "
            "back. Tagged audit, local, and Regulus costs reconcile within "
            "`max($0.000001, 0.5%)`; the shared provider-project window is used only as an "
            "upper-bound cross-check.\n"
        ),
    )
    return store.root


__all__ = [
    "ARM_PHRASE",
    "CAMPAIGN_CAP_USD",
    "CREDENTIAL_REFERENCE",
    "DEFAULT_CASES",
    "MODEL",
    "PER_RUN_CAP_USD",
    "CallObservation",
    "PaidAuthorization",
    "QualityVerdictObservation",
    "ReadinessPlan",
    "RecordedAgentCase",
    "RightsizingCapture",
    "RightsizingLiveHarness",
    "UsageObservation",
    "ValidationSummary",
    "load_recorded_cases",
    "seal_capture",
    "validate_capture",
]
