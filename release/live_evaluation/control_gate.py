"""Fail-closed local gates and one-shot paid probe coordination.

This module never resolves a credential and never performs network I/O.  It records
the pre-paid proofs and issues durable, single-use authorizations that an external
campaign driver may execute only after explicit paid-cost acknowledgement.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal, Protocol

from .config import CampaignConfig
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .ledger import CampaignLedger

ProbeKind = Literal["provider", "chroma"]
_LOGICAL_REFERENCE = re.compile(r"^[a-z][a-z0-9.-]{2,80}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOCAL_CRITERIA = (
    "control.tenant-budget-10",
    "control.run-budget-025",
    "control.budget-concurrency",
    "control.budget-rejection",
    "control.budget-commit-release",
    "control.budget-recovery",
    "control.audit-signed",
    "control.chroma-pinned-loopback",
    "control.chroma-corpus-seeded",
)


class ProbeAlreadyAuthorizedError(RuntimeError):
    """A paid probe has already crossed its durable one-shot boundary."""


@dataclass(frozen=True, slots=True)
class BudgetGateProof:
    tenant_limit_usd: Decimal
    run_limit_usd: Decimal
    concurrent_admission_proved: bool
    over_limit_rejected: bool
    commit_and_release_proved: bool
    restart_recovery_proved: bool
    evidence_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SignedAuditReadiness:
    state: Literal["signed"]
    algorithm: Literal["hmac-sha256"]
    signing_reference: str
    evidence_reference: str

    def __post_init__(self) -> None:
        if self.state != "signed" or self.algorithm != "hmac-sha256":
            raise ValueError("audit readiness must be signed with HMAC-SHA256")
        if self.signing_reference.startswith("sk-") or not _LOGICAL_REFERENCE.fullmatch(
            self.signing_reference
        ):
            raise ValueError("signing material must be identified by a logical reference")


@dataclass(frozen=True, slots=True)
class ChromaIdentity:
    image: str
    host: str
    port: int
    instance_id: str
    api_version: str
    evidence_reference: str

    def __post_init__(self) -> None:
        if self.image.endswith(":latest") or ":" not in self.image:
            raise ValueError("Chroma image must be pinned to an explicit version")
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as exc:
            raise ValueError("Chroma host must be a numeric loopback address") from exc
        if not address.is_loopback:
            raise ValueError("Chroma must bind to loopback")
        if not 1 <= self.port <= 65535:
            raise ValueError("Chroma port is invalid")
        if not self.instance_id or not self.api_version:
            raise ValueError("Chroma instance and API version are required")


@dataclass(frozen=True, slots=True)
class ChromaCorpusDocument:
    document_id: str
    tenant_id: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.document_id or not _SHA256.fullmatch(self.sha256):
            raise ValueError("corpus documents require an ID and lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PaidProbeAuthorization:
    kind: ProbeKind
    campaign_id: str
    tenant_id: str
    operation_id: str
    run_id: str
    credential_reference: str
    authorization_event_id: str


@dataclass(frozen=True, slots=True)
class PaidProbeResult:
    """Sanitized identities returned by an external instrumented probe executor."""

    kind: ProbeKind
    operation_id: str
    run_id: str
    audit_event_id: str
    cost_event_id: str
    provider_request_id: str | None
    connector_request_id: str | None
    request_count: int
    cache_hit: bool
    audit_chain_signed: bool
    cleanup_state: Literal["committed"]
    measured_cost_usd: Decimal

    def __post_init__(self) -> None:
        if self.kind not in {"provider", "chroma"}:
            raise ValueError("unsupported paid probe kind")
        if self.cleanup_state != "committed":
            raise ValueError("probe reservation must be committed")
        required_identities = (
            self.operation_id,
            self.run_id,
            self.audit_event_id,
            self.cost_event_id,
        )
        identities = required_identities + (
            (self.provider_request_id,) if self.provider_request_id is not None else ()
        )
        if any(not identity for identity in required_identities) or len(set(identities)) != len(
            identities
        ):
            raise ValueError("probe result identities must be nonempty and namespace-distinct")


class PaidProbeExecutor(Protocol):
    """External paid boundary; implementations must return sanitized identities only."""

    def execute_paid_probe(self, authorization: PaidProbeAuthorization) -> PaidProbeResult: ...


class ControlPlaneGate:
    """Record local readiness and coordinate exactly one paid probe of each kind."""

    def __init__(
        self,
        *,
        store: EvidenceStore,
        ledger: CampaignLedger,
        campaign: CampaignConfig,
    ) -> None:
        if ledger.store.root != store.root:
            raise ValueError("campaign ledger belongs to a different evidence bundle")
        if campaign.provider_secret_ref.startswith("sk-"):
            raise ValueError("campaign accepts a logical credential reference only")
        self.store = store
        self.ledger = ledger
        self.campaign = campaign

    def _validate_references(self, references: tuple[str, ...]) -> None:
        if not references:
            raise ValueError("gate proof requires durable evidence references")
        self.store.validate_evidence_references(
            (AcceptanceCriterion("control-gate-proof", "pass", references),)
        )

    def _record_criteria(self, criterion_ids: tuple[str, ...], reference: str) -> None:
        current = {item.criterion_id: item.status for item in self.ledger.criteria}
        for criterion_id in criterion_ids:
            if current[criterion_id] == "not_run":
                self.ledger.record(criterion_id, "pass", evidence=(reference,))
            elif current[criterion_id] != "pass":
                raise RuntimeError(f"control criterion is not admissible: {criterion_id}")

    def record_budget_gate(self, proof: BudgetGateProof) -> str:
        if self.campaign.campaign_budget_usd != Decimal(
            "10.00"
        ) or proof.tenant_limit_usd != Decimal("10.00"):
            raise ValueError("tenant campaign limit must be exactly $10.00")
        if self.campaign.per_run_cap_usd != Decimal("0.25") or proof.run_limit_usd != Decimal(
            "0.25"
        ):
            raise ValueError("run limit must be exactly $0.25")
        observations = (
            proof.concurrent_admission_proved,
            proof.over_limit_rejected,
            proof.commit_and_release_proved,
            proof.restart_recovery_proved,
        )
        if not all(value is True for value in observations):
            raise ValueError("every budget behavior must be proved before the gate opens")
        self._validate_references(proof.evidence_references)
        event_id = self.store.append_event(
            "control.budget-gate.recorded",
            {
                "tenant_limit_usd": "10.00",
                "run_limit_usd": "0.25",
                "concurrent_admission_proved": True,
                "over_limit_rejected": True,
                "commit_and_release_proved": True,
                "restart_recovery_proved": True,
                "evidence_references": list(proof.evidence_references),
            },
        )
        reference = f"events.ndjson#{event_id}"
        self._record_criteria(_LOCAL_CRITERIA[:6], reference)
        return reference

    def record_signed_audit_readiness(self, readiness: SignedAuditReadiness) -> str:
        self._validate_references((readiness.evidence_reference,))
        event_id = self.store.append_event(
            "control.signed-readiness.recorded",
            {
                "state": readiness.state,
                "algorithm": readiness.algorithm,
                "signing_reference": readiness.signing_reference,
                "evidence_reference": readiness.evidence_reference,
                "custody_scope": "local-keyed-integrity",
            },
        )
        reference = f"events.ndjson#{event_id}"
        self._record_criteria(("control.audit-signed",), reference)
        return reference

    def record_chroma_readiness(
        self,
        identity: ChromaIdentity,
        documents: tuple[ChromaCorpusDocument, ...],
    ) -> str:
        self._validate_references((identity.evidence_reference,))
        if len(documents) != 3:
            raise ValueError("Chroma corpus must contain exactly three documents")
        if (
            len({item.document_id for item in documents}) != 3
            or len({item.sha256 for item in documents}) != 3
        ):
            raise ValueError("Chroma corpus IDs and hashes must be unique")
        if any(item.tenant_id != self.campaign.tenant_id for item in documents):
            raise ValueError("Chroma corpus must be tenant scoped to the campaign")
        event_id = self.store.append_event(
            "control.chroma-readiness.recorded",
            {
                "identity": {
                    "image": identity.image,
                    "host": identity.host,
                    "port": identity.port,
                    "instance_id": identity.instance_id,
                    "api_version": identity.api_version,
                },
                "corpus": [asdict(item) for item in documents],
                "evidence_reference": identity.evidence_reference,
            },
        )
        reference = f"events.ndjson#{event_id}"
        self._record_criteria(
            ("control.chroma-pinned-loopback", "control.chroma-corpus-seeded"),
            reference,
        )
        return reference

    def _probe_events(self, kind: ProbeKind, suffix: str) -> tuple[dict[str, object], ...]:
        return tuple(
            event
            for event in self.store.read_events()
            if event.get("type") == f"control.probe.{suffix}"
            and isinstance(event.get("data"), dict)
            and event["data"].get("kind") == kind  # type: ignore[index]
        )

    def _claim_probe_once(self, kind: ProbeKind, phase: str) -> None:
        """Create a durable fail-closed marker before crossing a side-effect boundary."""
        if self.store.is_sealed:
            raise RuntimeError("evidence bundle is sealed")
        marker = self.store.root / f".control-probe-{kind}-{phase}"
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if phase == "authorized":
                raise ProbeAlreadyAuthorizedError(f"{kind} probe was already authorized") from exc
            raise ValueError(f"{kind} probe is already reconciled") from exc
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(self.store.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def authorize_paid_probe(
        self, *, kind: ProbeKind, operation_id: str, run_id: str
    ) -> PaidProbeAuthorization:
        if kind not in {"provider", "chroma"}:
            raise ValueError("unsupported paid probe kind")
        statuses = {item.criterion_id: item.status for item in self.ledger.criteria}
        missing = [
            criterion_id for criterion_id in _LOCAL_CRITERIA if statuses[criterion_id] != "pass"
        ]
        if missing:
            raise RuntimeError(f"local control-plane gates are incomplete: {', '.join(missing)}")
        if self._probe_events(kind, "authorized"):
            raise ProbeAlreadyAuthorizedError(f"{kind} probe was already authorized")
        if not operation_id or not run_id or operation_id == run_id:
            raise ValueError("probe operation and run identities must be distinct")
        self._claim_probe_once(kind, "authorized")
        event_id = self.store.append_event(
            "control.probe.authorized",
            {
                "kind": kind,
                "campaign_id": self.campaign.campaign_id,
                "tenant_id": self.campaign.tenant_id,
                "credential_reference": self.campaign.provider_secret_ref,
            },
            correlation=CorrelationIds(operation_id=operation_id, run_id=run_id),
        )
        return PaidProbeAuthorization(
            kind=kind,
            campaign_id=self.campaign.campaign_id,
            tenant_id=self.campaign.tenant_id,
            operation_id=operation_id,
            run_id=run_id,
            credential_reference=self.campaign.provider_secret_ref,
            authorization_event_id=event_id,
        )

    def reconcile_paid_probe(self, result: PaidProbeResult) -> str:
        authorized = self._probe_events(result.kind, "authorized")
        if len(authorized) != 1:
            raise RuntimeError(f"{result.kind} probe has no unique authorization")
        if self._probe_events(result.kind, "reconciled"):
            raise ValueError(f"{result.kind} probe is already reconciled")
        correlation = authorized[0].get("correlation")
        if (
            not isinstance(correlation, dict)
            or correlation.get("operation_id") != result.operation_id
            or correlation.get("run_id") != result.run_id
        ):
            raise ValueError("probe result identities do not match its authorization")
        if result.request_count != 1:
            raise ValueError("paid probe must report exactly one provider call")
        if result.cache_hit:
            raise ValueError("control-plane paid probe cannot be satisfied from cache")
        if not result.audit_chain_signed:
            raise ValueError("probe audit chain is not signed")
        if result.measured_cost_usd < 0 or result.measured_cost_usd > Decimal("0.25"):
            raise ValueError("probe cost is outside the admitted run limit")
        if result.kind == "chroma" and not result.connector_request_id:
            raise ValueError("Chroma probe requires a connector request identity")
        self._claim_probe_once(result.kind, "reconciled")
        event_id = self.store.append_event(
            "control.probe.reconciled",
            {
                "kind": result.kind,
                "connector_request_identity": result.connector_request_id,
                "request_count": 1,
                "cache_hit": False,
                "audit_chain_signed": True,
                "cleanup_state": result.cleanup_state,
                "measured_cost_usd": str(result.measured_cost_usd),
            },
            correlation=CorrelationIds(
                operation_id=result.operation_id,
                run_id=result.run_id,
                audit_event_id=result.audit_event_id,
                cost_event_id=result.cost_event_id,
                provider_request_id=result.provider_request_id,
            ),
        )
        reference = f"events.ndjson#{event_id}"
        if result.kind == "provider":
            self._record_criteria(
                (
                    "control.provider-credential-valid",
                    "control.provider-credential-not-persisted",
                    "control.provider-probe-reconciled",
                ),
                reference,
            )
        else:
            self._record_criteria(("control.chroma-probe-reconciled",), reference)
        if all(self._probe_events(kind, "reconciled") for kind in ("provider", "chroma")):
            self._record_criteria(("audit.probe-events-instrumented",), reference)
        return reference
