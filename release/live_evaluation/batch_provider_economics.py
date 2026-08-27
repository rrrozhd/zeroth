"""Provider-gated live harness for ``batching.provider-economics``.

The harness never resolves or accepts provider credentials.  It schedules an
operator-supplied live invocation boundary only after a non-secret readiness
attestation, exact cost acknowledgement, and campaign caps pass preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from .campaign_http import provider_acknowledgement
from .config import CampaignConfig
from .evidence import AcceptanceCriterion, EvidenceStore

CRITERION_ID = "batching.provider-economics"
REPETITIONS = 3
ITEMS_PER_REPETITION = 8
CONCURRENCY = 4
MAX_PER_RUN_USD = Decimal("0.25")
MAX_CAMPAIGN_USD = Decimal("10.00")
ABSOLUTE_TOLERANCE_USD = Decimal("0.000001")
RELATIVE_TOLERANCE = Decimal("0.005")


def _require_identifier(value: str, *, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"invalid {field}")


def _require_money(value: Decimal, *, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative Decimal")


def _require_int(value: int, *, field: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"invalid {field}")


def _outside_tolerance(left: Decimal, right: Decimal) -> bool:
    tolerance = max(
        ABSOLUTE_TOLERANCE_USD,
        max(abs(left), abs(right)) * RELATIVE_TOLERANCE,
    )
    return abs(left - right) > tolerance


@dataclass(frozen=True, slots=True)
class ReadinessAttestation:
    """Non-secret statement that the campaign's logical credential is installed."""

    campaign_id: str
    tenant_id: str
    logical_secret_ref: str
    installed: bool
    provider_probe_reconciled: bool
    provider_request_id: str | None
    operation_id: str
    run_id: str
    audit_event_id: str
    cost_event_id: str
    measured_cost_usd: Decimal
    campaign_spend_before_usd: Decimal
    audit_signed: bool

    def __post_init__(self) -> None:
        for field in (
            "campaign_id",
            "tenant_id",
            "logical_secret_ref",
            "operation_id",
            "run_id",
            "audit_event_id",
            "cost_event_id",
        ):
            _require_identifier(getattr(self, field), field=field)
        if self.provider_request_id is not None:
            _require_identifier(self.provider_request_id, field="provider_request_id")
        if not isinstance(self.installed, bool) or not isinstance(
            self.provider_probe_reconciled, bool
        ):
            raise ValueError("readiness states must be boolean")
        if not isinstance(self.audit_signed, bool):
            raise ValueError("audit_signed must be boolean")
        _require_money(self.measured_cost_usd, field="measured_cost_usd")
        _require_money(self.campaign_spend_before_usd, field="campaign_spend_before_usd")
        if self.campaign_spend_before_usd < self.measured_cost_usd:
            raise ValueError("campaign spend cannot be below the readiness probe cost")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ReadinessAttestation:
        """Parse the JSON representation without permitting coercion of readiness flags."""
        payload = dict(value)
        try:
            payload["measured_cost_usd"] = Decimal(str(payload["measured_cost_usd"]))
            payload["campaign_spend_before_usd"] = Decimal(
                str(payload["campaign_spend_before_usd"])
            )
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid readiness cost") from exc
        return cls(**payload)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LiveBatchGate:
    """All operator-controlled gates required before a paid call may start."""

    campaign: CampaignConfig
    provider_execution_enabled: bool
    external_cost_acknowledgement: str | None
    readiness: ReadinessAttestation

    def validate(self) -> None:
        if not self.provider_execution_enabled:
            raise RuntimeError("live provider execution is disabled")
        if self.external_cost_acknowledgement != provider_acknowledgement(
            self.campaign.campaign_id
        ):
            raise RuntimeError("live provider cost acknowledgement is not exact")
        expected_readiness = (
            self.campaign.campaign_id,
            self.campaign.tenant_id,
            self.campaign.provider_secret_ref,
        )
        observed_readiness = (
            self.readiness.campaign_id,
            self.readiness.tenant_id,
            self.readiness.logical_secret_ref,
        )
        if observed_readiness != expected_readiness:
            raise RuntimeError("provider readiness identity does not match the campaign")
        if not self.readiness.installed:
            raise RuntimeError("provider credential is not installed")
        if (
            not self.readiness.provider_probe_reconciled
            or not self.readiness.audit_signed
            or self.readiness.measured_cost_usd > MAX_PER_RUN_USD
            or self.readiness.campaign_spend_before_usd > MAX_CAMPAIGN_USD
        ):
            raise RuntimeError("provider readiness probe is not reconciled and signed")
        probe_ids = tuple(
            value
            for value in (
                self.readiness.provider_request_id,
                self.readiness.operation_id,
                self.readiness.run_id,
                self.readiness.audit_event_id,
                self.readiness.cost_event_id,
            )
            if value is not None
        )
        if len(set(probe_ids)) != len(probe_ids):
            raise RuntimeError("provider readiness probe identities alias across namespaces")
        if self.campaign.per_run_cap_usd != MAX_PER_RUN_USD:
            raise RuntimeError("per-run cap must be exactly $0.25")
        if self.campaign.campaign_budget_usd != MAX_CAMPAIGN_USD:
            raise RuntimeError("campaign cap must be exactly $10.00")
        worst_case_campaign_cost = MAX_PER_RUN_USD * REPETITIONS
        if self.readiness.campaign_spend_before_usd + worst_case_campaign_cost > MAX_CAMPAIGN_USD:
            raise RuntimeError("campaign lacks worst-case headroom for three parent reservations")


@dataclass(frozen=True, slots=True)
class PlannedBatchSubmission:
    campaign_id: str
    repetition: int
    items: int
    concurrency: int
    per_run_cap_usd: Decimal
    campaign_cap_usd: Decimal

    def __post_init__(self) -> None:
        _require_int(
            self.repetition,
            field="repetition",
            minimum=1,
            maximum=REPETITIONS,
        )
        if self.items != ITEMS_PER_REPETITION:
            raise ValueError("batch submission must contain exactly eight items")
        if self.concurrency != CONCURRENCY:
            raise ValueError("batch submission concurrency must be exactly four")
        if self.per_run_cap_usd != MAX_PER_RUN_USD:
            raise ValueError("batch submission per-run cap must be exactly $0.25")
        if self.campaign_cap_usd != MAX_CAMPAIGN_USD:
            raise ValueError("batch submission campaign cap must be exactly $10.00")
        _require_identifier(self.campaign_id, field="campaign_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "repetition": self.repetition,
            "items": self.items,
            "concurrency": self.concurrency,
            "per_run_cap_usd": format(self.per_run_cap_usd, "f"),
            "campaign_cap_usd": format(self.campaign_cap_usd, "f"),
        }


@dataclass(frozen=True, slots=True)
class BatchEconomicsPlan:
    criterion_id: str
    campaign_id: str
    repetitions: int
    items_per_repetition: int
    concurrency: int
    per_run_cap_usd: Decimal
    campaign_cap_usd: Decimal
    campaign_spend_before_usd: Decimal
    submissions: tuple[PlannedBatchSubmission, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "criterion_id": self.criterion_id,
            "campaign_id": self.campaign_id,
            "repetitions": self.repetitions,
            "items_per_repetition": self.items_per_repetition,
            "concurrency": self.concurrency,
            "per_run_cap_usd": format(self.per_run_cap_usd, "f"),
            "campaign_cap_usd": format(self.campaign_cap_usd, "f"),
            "campaign_spend_before_usd": format(self.campaign_spend_before_usd, "f"),
            "submissions": [submission.as_dict() for submission in self.submissions],
        }


@dataclass(frozen=True, slots=True)
class BatchEconomicsObservation:
    """Sanitized identities and amounts returned by one live child invocation."""

    campaign_id: str
    repetition: int
    item_index: int
    parent_run_id: str
    child_run_id: str
    operation_id: str
    provider_request_id: str | None
    audit_event_id: str
    cost_event_ids: tuple[str, ...]
    reservation_id: str
    reservation_operation_id: str
    reservation_status: str
    reserved_max_cost_usd: Decimal
    reservation_actual_cost_usd: Decimal
    reservation_released_cost_usd: Decimal
    reservation_cleanup_status: str
    cache_hit: bool
    audit_cost_usd: Decimal
    run_cost_usd: Decimal
    local_cost_usd: Decimal
    economics_cost_usd: Decimal
    audit_signed: bool
    audit_chain_verified: bool
    parent_child_linked: bool

    def __post_init__(self) -> None:
        for field in (
            "campaign_id",
            "parent_run_id",
            "child_run_id",
            "operation_id",
            "audit_event_id",
            "reservation_id",
            "reservation_operation_id",
            "reservation_status",
            "reservation_cleanup_status",
        ):
            _require_identifier(getattr(self, field), field=field)
        if self.provider_request_id is not None:
            _require_identifier(self.provider_request_id, field="provider_request_id")
        for cost_event_id in self.cost_event_ids:
            _require_identifier(cost_event_id, field="cost_event_id")
        _require_int(
            self.repetition,
            field="repetition",
            minimum=1,
            maximum=REPETITIONS,
        )
        _require_int(
            self.item_index,
            field="item_index",
            minimum=0,
            maximum=ITEMS_PER_REPETITION - 1,
        )
        for field in (
            "cache_hit",
            "audit_signed",
            "audit_chain_verified",
            "parent_child_linked",
        ):
            if not isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be boolean")
        for field in (
            "audit_cost_usd",
            "run_cost_usd",
            "local_cost_usd",
            "economics_cost_usd",
            "reserved_max_cost_usd",
            "reservation_actual_cost_usd",
            "reservation_released_cost_usd",
        ):
            _require_money(getattr(self, field), field=field)

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        for field in (
            "audit_cost_usd",
            "run_cost_usd",
            "local_cost_usd",
            "economics_cost_usd",
            "reserved_max_cost_usd",
            "reservation_actual_cost_usd",
            "reservation_released_cost_usd",
        ):
            value[field] = format(getattr(self, field), "f")
        return value


@dataclass(frozen=True, slots=True)
class ParentBatchObservation:
    """One completed parent run and the service-minted evidence it owns."""

    campaign_id: str
    repetition: int
    parent_run_id: str
    status: str
    configured_concurrency: int
    observed_peak_concurrency: int
    campaign_spend_after_usd: Decimal
    audit_cost_usd: Decimal
    run_cost_usd: Decimal
    local_cost_usd: Decimal
    economics_cost_usd: Decimal
    audit_signed: bool
    audit_chain_verified: bool
    children: tuple[BatchEconomicsObservation, ...]

    def __post_init__(self) -> None:
        for field in ("campaign_id", "parent_run_id", "status"):
            _require_identifier(getattr(self, field), field=field)
        for field in (
            "campaign_spend_after_usd",
            "audit_cost_usd",
            "run_cost_usd",
            "local_cost_usd",
            "economics_cost_usd",
        ):
            _require_money(getattr(self, field), field=field)
        _require_int(
            self.repetition,
            field="repetition",
            minimum=1,
            maximum=REPETITIONS,
        )
        _require_int(
            self.configured_concurrency,
            field="configured_concurrency",
            minimum=1,
            maximum=CONCURRENCY,
        )
        _require_int(
            self.observed_peak_concurrency,
            field="observed_peak_concurrency",
            minimum=1,
            maximum=CONCURRENCY,
        )
        for field in (
            "audit_signed",
            "audit_chain_verified",
        ):
            if not isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be boolean")
        if not isinstance(self.children, tuple):
            raise ValueError("children must be a tuple")

    def as_dict(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "repetition": self.repetition,
            "parent_run_id": self.parent_run_id,
            "status": self.status,
            "configured_concurrency": self.configured_concurrency,
            "observed_peak_concurrency": self.observed_peak_concurrency,
            "campaign_spend_after_usd": format(self.campaign_spend_after_usd, "f"),
            "audit_cost_usd": format(self.audit_cost_usd, "f"),
            "run_cost_usd": format(self.run_cost_usd, "f"),
            "local_cost_usd": format(self.local_cost_usd, "f"),
            "economics_cost_usd": format(self.economics_cost_usd, "f"),
            "audit_signed": self.audit_signed,
            "audit_chain_verified": self.audit_chain_verified,
            "children": [child.as_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class BatchEconomicsResult:
    plan: BatchEconomicsPlan
    parent_observations: tuple[ParentBatchObservation, ...]
    total_cost_usd: Decimal
    campaign_total_cost_usd: Decimal
    parent_run_totals_usd: Mapping[str, Decimal]
    passed: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "criterion_id": self.plan.criterion_id,
            "campaign_id": self.plan.campaign_id,
            "passed": self.passed,
            "configured_repetitions": self.plan.repetitions,
            "configured_items_per_repetition": self.plan.items_per_repetition,
            "configured_concurrency": self.plan.concurrency,
            "per_run_cap_usd": format(self.plan.per_run_cap_usd, "f"),
            "campaign_cap_usd": format(self.plan.campaign_cap_usd, "f"),
            "tolerance": "max(0.000001 USD, 0.5 percent)",
            "total_cost_usd": format(self.total_cost_usd, "f"),
            "campaign_total_cost_usd": format(self.campaign_total_cost_usd, "f"),
            "parent_run_totals_usd": {
                key: format(value, "f") for key, value in sorted(self.parent_run_totals_usd.items())
            },
            "parent_observations": [item.as_dict() for item in self.parent_observations],
        }


class LiveBatchAdapter(Protocol):
    """Boundary implemented by the real Zeroth service client.

    The service, not this harness, owns parent, child, operation, audit, provider,
    and cost identities.
    """

    async def submit_parent(self, submission: PlannedBatchSubmission) -> ParentBatchObservation: ...


class BatchProviderEconomicsHarness:
    """Run and seal the exact provider-backed batch economics criterion."""

    def __init__(self, gate: LiveBatchGate) -> None:
        self.gate = gate

    def dry_run(self) -> BatchEconomicsPlan:
        """Validate readiness without resolving a secret or performing network I/O."""
        self.gate.validate()
        campaign_id = self.gate.campaign.campaign_id
        submissions = tuple(
            PlannedBatchSubmission(
                campaign_id=campaign_id,
                repetition=repetition,
                items=ITEMS_PER_REPETITION,
                concurrency=CONCURRENCY,
                per_run_cap_usd=self.gate.campaign.per_run_cap_usd,
                campaign_cap_usd=self.gate.campaign.campaign_budget_usd,
            )
            for repetition in range(1, REPETITIONS + 1)
        )
        return BatchEconomicsPlan(
            criterion_id=CRITERION_ID,
            campaign_id=campaign_id,
            repetitions=REPETITIONS,
            items_per_repetition=ITEMS_PER_REPETITION,
            concurrency=CONCURRENCY,
            per_run_cap_usd=self.gate.campaign.per_run_cap_usd,
            campaign_cap_usd=self.gate.campaign.campaign_budget_usd,
            campaign_spend_before_usd=self.gate.readiness.campaign_spend_before_usd,
            submissions=submissions,
        )

    async def execute(self, adapter: LiveBatchAdapter) -> BatchEconomicsResult:
        """Submit exactly three parents; Zeroth owns child scheduling and identities."""
        plan = self.dry_run()
        observations: list[ParentBatchObservation] = []
        for submission in plan.submissions:
            observation = await adapter.submit_parent(submission)
            if not isinstance(observation, ParentBatchObservation):
                raise TypeError("adapter returned an invalid parent observation")
            observations.append(observation)
        return self._reconcile(plan, observations)

    @staticmethod
    def _reconcile(
        plan: BatchEconomicsPlan,
        observations: Sequence[ParentBatchObservation],
    ) -> BatchEconomicsResult:
        if len(observations) != len(plan.submissions):
            raise RuntimeError("live result does not contain every parent submission")
        provider_ids: list[str] = []
        audit_ids: list[str] = []
        cost_ids: list[str] = []
        operation_ids: list[str] = []
        child_run_ids: list[str] = []
        parent_run_ids: list[str] = []
        reservation_ids: list[str] = []
        parent_totals: dict[str, Decimal] = {}
        totals = {
            "audit": Decimal(0),
            "run": Decimal(0),
            "local": Decimal(0),
            "economics": Decimal(0),
        }

        campaign_spend = plan.campaign_spend_before_usd
        for submission, parent in zip(plan.submissions, observations, strict=True):
            if (parent.campaign_id, parent.repetition) != (
                submission.campaign_id,
                submission.repetition,
            ):
                raise RuntimeError("parent campaign/repetition identity reconciliation failed")
            if parent.status != "succeeded":
                raise RuntimeError("parent run did not succeed")
            if (
                parent.configured_concurrency != submission.concurrency
                or parent.observed_peak_concurrency != submission.concurrency
            ):
                raise RuntimeError("parent run did not observe exact concurrency four")
            if not parent.audit_signed or not parent.audit_chain_verified:
                raise RuntimeError("parent audit chain is not signed and verified")
            if len(parent.children) != submission.items:
                raise RuntimeError("parent run does not contain exactly eight children")
            if {child.item_index for child in parent.children} != set(range(submission.items)):
                raise RuntimeError("parent child item indexes are not exact")
            child_totals = {key: Decimal(0) for key in totals}
            parent_reserved_maximum = Decimal(0)
            for child in parent.children:
                if (
                    child.campaign_id != submission.campaign_id
                    or child.repetition != submission.repetition
                    or child.parent_run_id != parent.parent_run_id
                    or not child.parent_child_linked
                ):
                    raise RuntimeError("parent/child/run/operation identity reconciliation failed")
                if not child.audit_signed or not child.audit_chain_verified:
                    raise RuntimeError("child audit chain is not signed and verified")
                if child.reservation_operation_id != child.operation_id:
                    raise RuntimeError("child reservation identity does not match operation")
                if child.reservation_cleanup_status != "complete":
                    raise RuntimeError("child reservation cleanup is incomplete")
                if child.cache_hit:
                    if child.reservation_status != "released" or (
                        child.provider_request_id is not None
                        or child.cost_event_ids
                        or any(
                            value != 0
                            for value in (
                                child.audit_cost_usd,
                                child.run_cost_usd,
                                child.local_cost_usd,
                                child.economics_cost_usd,
                                child.reserved_max_cost_usd,
                                child.reservation_actual_cost_usd,
                                child.reservation_released_cost_usd,
                            )
                        )
                    ):
                        raise RuntimeError("cache hit has provider cost activity")
                else:
                    if len(child.cost_event_ids) != 1:
                        raise RuntimeError(
                            "non-cache provider call must have exactly one cost event"
                        )
                    if child.reservation_status != "committed":
                        raise RuntimeError("child reservation was not committed")
                    if _outside_tolerance(child.reservation_actual_cost_usd, child.audit_cost_usd):
                        raise RuntimeError("child reservation actual cost does not reconcile")
                    if child.reservation_actual_cost_usd > child.reserved_max_cost_usd:
                        raise RuntimeError("child spend exceeds its reservation")
                    expected_release = (
                        child.reserved_max_cost_usd - child.reservation_actual_cost_usd
                    )
                    if _outside_tolerance(expected_release, child.reservation_released_cost_usd):
                        raise RuntimeError("child reservation remainder was not released")
                    parent_reserved_maximum += child.reserved_max_cost_usd
                    if child.provider_request_id is not None:
                        provider_ids.append(child.provider_request_id)
                    cost_ids.extend(child.cost_event_ids)
                    reservation_ids.append(child.reservation_id)

                operation_ids.append(child.operation_id)
                child_run_ids.append(child.child_run_id)
                audit_ids.append(child.audit_event_id)
                values = {
                    "audit": child.audit_cost_usd,
                    "run": child.run_cost_usd,
                    "local": child.local_cost_usd,
                    "economics": child.economics_cost_usd,
                }
                for field, value in values.items():
                    child_totals[field] += value
                if any(_outside_tolerance(values["audit"], value) for value in values.values()):
                    raise RuntimeError("audit/run/local/economics child totals exceed tolerance")

            parent_values = {
                "audit": parent.audit_cost_usd,
                "run": parent.run_cost_usd,
                "local": parent.local_cost_usd,
                "economics": parent.economics_cost_usd,
            }
            if parent_reserved_maximum > plan.per_run_cap_usd:
                raise RuntimeError("child reservations exceed the parent per-run cap")
            for field, value in parent_values.items():
                if _outside_tolerance(child_totals[field], value):
                    raise RuntimeError("child and parent plane totals exceed tolerance")
                totals[field] += value
            if any(
                _outside_tolerance(parent_values["audit"], value)
                for value in parent_values.values()
            ):
                raise RuntimeError("audit/run/local/economics parent totals exceed tolerance")
            campaign_spend += parent.audit_cost_usd
            if _outside_tolerance(campaign_spend, parent.campaign_spend_after_usd):
                raise RuntimeError("campaign spend progression does not reconcile")
            if campaign_spend > plan.campaign_cap_usd:
                raise RuntimeError("observed spend exceeds the campaign cap")
            parent_totals[parent.parent_run_id] = parent.audit_cost_usd
            parent_run_ids.append(parent.parent_run_id)

        for label, values in (
            ("provider request", provider_ids),
            ("audit event", audit_ids),
            ("cost event", cost_ids),
            ("operation", operation_ids),
            ("child run", child_run_ids),
            ("parent run", parent_run_ids),
            ("reservation", reservation_ids),
        ):
            if any(count != 1 for count in Counter(values).values()):
                raise RuntimeError(f"duplicate {label} identity")
        if any(value > plan.per_run_cap_usd for value in parent_totals.values()):
            raise RuntimeError("observed spend exceeds the per-run cap")
        campaign_total = campaign_spend
        if any(_outside_tolerance(totals["audit"], value) for value in totals.values()):
            raise RuntimeError("audit/run/local/economics campaign totals exceed tolerance")

        return BatchEconomicsResult(
            plan=plan,
            parent_observations=tuple(observations),
            total_cost_usd=totals["audit"],
            campaign_total_cost_usd=campaign_total,
            parent_run_totals_usd=parent_totals,
        )

    def seal(self, result: BatchEconomicsResult, destination: Path) -> Path:
        """Validate, secret-scan, and irreversibly checksum one exact-criterion bundle."""
        if not result.passed or result.plan.criterion_id != CRITERION_ID:
            raise RuntimeError("only a passing exact criterion result may be sealed")
        if result.plan != self.dry_run():
            raise RuntimeError("result plan does not match the gated live plan")
        # Re-run reconciliation so callers cannot construct an inconsistent result.
        reconciled = self._reconcile(result.plan, result.parent_observations)
        if (
            reconciled.total_cost_usd != result.total_cost_usd
            or reconciled.campaign_total_cost_usd != result.campaign_total_cost_usd
        ):
            raise RuntimeError("result total changed after reconciliation")
        destination = destination.expanduser().absolute()
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.", dir=destination.parent
        ) as temporary:
            staging = Path(temporary) / "bundle"
            store = EvidenceStore(staging)
            store.write_manifest(
                {
                    "schema_version": 1,
                    "checkpoint": "batch-provider-economics-live",
                    "criterion_id": CRITERION_ID,
                    "campaign_id": result.plan.campaign_id,
                    "parent_runs_planned": len(result.plan.submissions),
                    "child_observations_required": (
                        result.plan.repetitions * result.plan.items_per_repetition
                    ),
                    "provider_execution_enabled": True,
                    "credential_readiness_attested": True,
                    "credential_value_retained": False,
                    "provider_readiness_probe": {
                        "provider_request_id": self.gate.readiness.provider_request_id,
                        "operation_id": self.gate.readiness.operation_id,
                        "run_id": self.gate.readiness.run_id,
                        "audit_event_id": self.gate.readiness.audit_event_id,
                        "cost_event_id": self.gate.readiness.cost_event_id,
                        "measured_cost_usd": format(self.gate.readiness.measured_cost_usd, "f"),
                        "audit_signed": True,
                        "reconciled": True,
                    },
                }
            )
            store._write_exclusive(  # repository-local primitive; still secret-scanned
                Path("reconciliation/batch-provider-economics.json"), result.as_dict()
            )
            store.finalize_bundle(
                acceptance=(
                    AcceptanceCriterion(
                        CRITERION_ID,
                        "pass",
                        ("reconciliation/batch-provider-economics.json",),
                    ),
                ),
                report_markdown=(
                    "# Batch provider economics\n\n"
                    "Three eight-item repetitions ran with concurrency four. Every non-cache "
                    "provider call has exactly one cost event, signed audit evidence, and "
                    "matching parent/child/run/operation identities. Audit, run, local, and "
                    "economics totals reconcile within max($0.000001, 0.5%), under the $0.25 "
                    "per-run and $10 campaign ceilings. No credential value is retained.\n"
                ),
            )
            verify_sealed_bundle(staging)
            staging.replace(destination)
        return destination


def verify_sealed_bundle(root: Path) -> dict[str, object]:
    """Verify path safety, secret safety, checksums, and exact acceptance criterion."""
    root = root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("sealed bundle root is unsafe")
    root = root.resolve(strict=True)
    sums_path = root / "SHA256SUMS"
    if not sums_path.is_file() or sums_path.is_symlink():
        raise RuntimeError("checksum manifest is missing")
    expected_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != sums_path
    }
    observed_files: set[str] = set()
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        candidate = Path(relative)
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or candidate.is_absolute()
            or ".." in candidate.parts
            or relative in observed_files
        ):
            raise RuntimeError("checksum manifest contains an invalid entry")
        target = root / candidate
        if target.is_symlink() or not target.is_file():
            raise RuntimeError("checksum target is missing or unsafe")
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"checksum mismatch: {relative}")
        observed_files.add(relative)
    if observed_files != expected_files:
        raise RuntimeError("checksum manifest does not cover the exact bundle")
    store = EvidenceStore(root)
    store.scan_recursive()
    try:
        acceptance = json.loads((root / "acceptance.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed acceptance is invalid") from exc
    criteria = acceptance.get("criteria") if isinstance(acceptance, Mapping) else None
    if (
        not isinstance(criteria, list)
        or len(criteria) != 1
        or not isinstance(criteria[0], Mapping)
        or criteria[0].get("criterion_id") != CRITERION_ID
        or criteria[0].get("status") != "pass"
    ):
        raise RuntimeError("sealed bundle does not contain the exact passing criterion")
    return {"verified": True, "criterion_id": CRITERION_ID, "files": len(observed_files)}


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Offer credential-free dry-run/readiness and sealed-bundle verification paths."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--campaign-config", type=Path, required=True)
    dry_run.add_argument("--readiness-attestation", type=Path, required=True)
    dry_run.add_argument("--enable-provider-execution", action="store_true")
    dry_run.add_argument("--acknowledge-external-cost", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        print(json.dumps(verify_sealed_bundle(args.bundle), sort_keys=True))
        return 0
    campaign = CampaignConfig.model_validate(
        _read_object(args.campaign_config, label="campaign config")
    )
    readiness = ReadinessAttestation.from_mapping(
        _read_object(args.readiness_attestation, label="readiness attestation")
    )
    plan = BatchProviderEconomicsHarness(
        LiveBatchGate(
            campaign=campaign,
            provider_execution_enabled=args.enable_provider_execution,
            external_cost_acknowledgement=args.acknowledge_external_cost,
            readiness=readiness,
        )
    ).dry_run()
    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
