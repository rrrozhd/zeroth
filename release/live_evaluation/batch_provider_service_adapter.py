"""Loopback-only public-service adapter for provider-backed parent batch runs.

The adapter submits only through ``POST /v1/runs`` and reads run, lineage,
evidence, and signed-chain state through public APIs. Product-owned identities
that are not public are supplied by an injected sanitized authoritative
collector; the adapter never invents them or reads a provider credential.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json as json_module
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .batch_provider_economics import (
    CONCURRENCY,
    ITEMS_PER_REPETITION,
    MAX_CAMPAIGN_USD,
    MAX_PER_RUN_USD,
    BatchEconomicsObservation,
    ParentBatchObservation,
    PlannedBatchSubmission,
)

ARM_PHRASE = "AUTHORIZE_BATCH_PROVIDER_SPEND"
PRODUCT_API_GAPS = (
    "public run APIs do not expose configured or observed peak branch concurrency",
    "public run evidence does not expose provider request, operation, reservation, "
    "or Regulus execution identities",
)
_ABSOLUTE_TOLERANCE = Decimal("0.000001")
_RELATIVE_TOLERANCE = Decimal("0.005")


def _opaque(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError(f"invalid {field}")


def _money(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite nonnegative Decimal")


def _integer(value: int, field: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"invalid {field}")


def _outside_tolerance(left: Decimal, right: Decimal) -> bool:
    tolerance = max(
        _ABSOLUTE_TOLERANCE,
        max(abs(left), abs(right)) * _RELATIVE_TOLERANCE,
    )
    return abs(left - right) > tolerance


def _loopback_origin(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        is_loopback = hostname == "localhost" or (
            hostname is not None and ipaddress.ip_address(hostname).is_loopback
        )
    except ValueError as exc:
        raise ValueError("batch service base URL must be a loopback origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("batch service base URL must be a loopback origin")
    return base_url.rstrip("/")


@dataclass(frozen=True, slots=True)
class BatchCollectionIdentity:
    """Public service identities authorizing one scoped collector read."""

    tenant_id: str
    campaign_id: str
    repetition: int
    parent_run_id: str
    child_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("tenant_id", "campaign_id", "parent_run_id"):
            _opaque(getattr(self, field), field)
        _integer(self.repetition, "repetition", 1, 3)
        if len(self.child_run_ids) != ITEMS_PER_REPETITION:
            raise ValueError("collector identity requires exactly eight child runs")
        for run_id in self.child_run_ids:
            _opaque(run_id, "child_run_id")
        if len(set(self.child_run_ids)) != len(self.child_run_ids):
            raise ValueError("collector identity contains duplicate child runs")


@dataclass(frozen=True, slots=True)
class CollectedChildReconciliation:
    """Sanitized authoritative cross-plane record for one real child call."""

    item_index: int
    child_run_id: str
    operation_id: str
    provider_request_id: str | None
    audit_event_id: str
    cost_event_id: str
    regulus_execution_event_id: str
    reservation_id: str
    reservation_operation_id: str
    reservation_status: str
    reserved_max_cost_usd: Decimal
    reservation_actual_cost_usd: Decimal
    reservation_released_cost_usd: Decimal
    reservation_cleanup_status: str
    cache_hit: bool
    audit_cost_usd: Decimal
    local_cost_usd: Decimal
    economics_cost_usd: Decimal

    def __post_init__(self) -> None:
        _integer(
            self.item_index,
            "item_index",
            0,
            ITEMS_PER_REPETITION - 1,
        )
        for field in (
            "child_run_id",
            "operation_id",
            "audit_event_id",
            "cost_event_id",
            "regulus_execution_event_id",
            "reservation_id",
            "reservation_operation_id",
            "reservation_status",
            "reservation_cleanup_status",
        ):
            _opaque(getattr(self, field), field)
        if self.provider_request_id is not None:
            _opaque(self.provider_request_id, "provider_request_id")
        if not isinstance(self.cache_hit, bool):
            raise ValueError("cache_hit must be boolean")
        for field in (
            "reserved_max_cost_usd",
            "reservation_actual_cost_usd",
            "reservation_released_cost_usd",
            "audit_cost_usd",
            "local_cost_usd",
            "economics_cost_usd",
        ):
            _money(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class CollectedParentReconciliation:
    """Sanitized concurrency and economics proof unavailable on public routes."""

    campaign_id: str
    repetition: int
    parent_run_id: str
    configured_concurrency: int
    observed_peak_concurrency: int
    campaign_spend_after_usd: Decimal
    children: tuple[CollectedChildReconciliation, ...]

    def __post_init__(self) -> None:
        for field in ("campaign_id", "parent_run_id"):
            _opaque(getattr(self, field), field)
        _integer(self.repetition, "repetition", 1, 3)
        _integer(
            self.configured_concurrency,
            "configured_concurrency",
            1,
            CONCURRENCY,
        )
        _integer(
            self.observed_peak_concurrency,
            "observed_peak_concurrency",
            1,
            CONCURRENCY,
        )
        _money(self.campaign_spend_after_usd, "campaign_spend_after_usd")
        if not isinstance(self.children, tuple):
            raise ValueError("children must be a tuple")


class AuthoritativeReconciliationCollector(Protocol):
    """Read sanitized reservation, local, Regulus, and provider identities."""

    def collect(self, identity: BatchCollectionIdentity) -> CollectedParentReconciliation: ...


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> object: ...


class AsyncHttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object] | None,
        timeout: float,
    ) -> HttpResponse: ...


@dataclass(slots=True)
class _UrllibResponse:
    status_code: int
    body: bytes

    def json(self) -> object:
        return json_module.loads(self.body)


class _UrllibTransport:
    """Dependency-light concrete transport; blocking work runs off the event loop."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object] | None,
        timeout: float,
    ) -> _UrllibResponse:
        return await asyncio.to_thread(
            self._request,
            method,
            url,
            headers,
            json,
            timeout,
        )

    @staticmethod
    def _request(
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None,
        timeout: float,
    ) -> _UrllibResponse:
        request = Request(
            url,
            data=(
                json_module.dumps(payload, separators=(",", ":")).encode("utf-8")
                if payload is not None
                else None
            ),
            headers=dict(headers),
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback checked
                return _UrllibResponse(response.status, response.read())
        except HTTPError as exc:
            return _UrllibResponse(exc.code, exc.read())


@dataclass(frozen=True, slots=True)
class _PublicChild:
    run_id: str
    evidence: Mapping[str, object]
    run_cost_usd: Decimal


class BatchProviderServiceAdapter:
    """Submit one parent and reconcile its real child calls without minting IDs."""

    def __init__(
        self,
        *,
        base_url: str,
        tenant_id: str,
        items: Sequence[Mapping[str, object]],
        arm: str,
        provider_ready: Callable[[], bool],
        auth_source: Callable[[], str],
        reconciliation_collector: AuthoritativeReconciliationCollector,
        http: AsyncHttpTransport | None = None,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 0.2,
        max_poll_attempts: int = 600,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base_url = _loopback_origin(base_url)
        _opaque(tenant_id, "tenant_id")
        if len(items) != ITEMS_PER_REPETITION:
            raise ValueError("batch adapter requires exactly eight input items")
        normalized_items: list[dict[str, object]] = []
        for expected_index, item in enumerate(items):
            if not isinstance(item, Mapping) or item.get("index") != expected_index:
                raise ValueError("batch adapter items require exact ordered indexes")
            normalized_items.append(dict(item))
        if not isinstance(arm, str):
            raise ValueError("arm must be a string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be nonnegative")
        _integer(max_poll_attempts, "max_poll_attempts", 1, 10000)
        self._tenant_id = tenant_id
        self._items = tuple(normalized_items)
        self._arm = arm
        self._provider_ready = provider_ready
        self._auth_source = auth_source
        self._collector = reconciliation_collector
        self._http = http if http is not None else _UrllibTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_poll_attempts = max_poll_attempts
        self._sleeper = sleeper

    async def submit_parent(self, submission: PlannedBatchSubmission) -> ParentBatchObservation:
        """Submit and reconcile one real parent batch run through loopback APIs."""
        self._gate(submission)
        try:
            service_auth = self._auth_source()
        except Exception:
            raise PermissionError("ephemeral service authentication is unavailable") from None
        if not isinstance(service_auth, str) or not service_auth:
            raise PermissionError("ephemeral service authentication is unavailable")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": service_auth,
            "X-Tenant-ID": self._tenant_id,
        }
        try:
            public = await self._collect_public(submission, headers)
        finally:
            service_auth = ""
            headers.clear()

        identity = BatchCollectionIdentity(
            tenant_id=self._tenant_id,
            campaign_id=submission.campaign_id,
            repetition=submission.repetition,
            parent_run_id=public["parent_run_id"],
            child_run_ids=tuple(child.run_id for child in public["children"]),
        )
        try:
            collected = self._collector.collect(identity)
        except Exception:
            raise RuntimeError("authoritative reconciliation collection failed") from None
        if not isinstance(collected, CollectedParentReconciliation):
            raise TypeError("authoritative collector returned an invalid result")
        return self._build_observation(
            submission=submission,
            identity=identity,
            collected=collected,
            public_children=public["children"],
        )

    def _gate(self, submission: PlannedBatchSubmission) -> None:
        if not isinstance(submission, PlannedBatchSubmission):
            raise TypeError("submission must be a PlannedBatchSubmission")
        if self._arm != ARM_PHRASE:
            raise PermissionError("live batch provider execution is not explicitly armed")
        try:
            provider_ready = self._provider_ready()
        except Exception:
            raise PermissionError("opaque provider readiness is not confirmed") from None
        if provider_ready is not True:
            raise PermissionError("opaque provider readiness is not confirmed")
        if (
            submission.items != ITEMS_PER_REPETITION
            or submission.concurrency != CONCURRENCY
            or submission.per_run_cap_usd != MAX_PER_RUN_USD
            or submission.campaign_cap_usd != MAX_CAMPAIGN_USD
        ):
            raise PermissionError("batch submission readiness or caps are not exact")

    async def _collect_public(
        self,
        submission: PlannedBatchSubmission,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        created = await self._request_object(
            "POST",
            "/v1/runs",
            headers=headers,
            payload={
                "input_payload": {"items": [dict(item) for item in self._items]},
                "campaign_id": submission.campaign_id,
                "campaign_strict": True,
            },
            expected=202,
            label="parent submission",
        )
        parent_run_id = self._string(created, "run_id", "parent submission")
        if (
            created.get("campaign_id") != submission.campaign_id
            or created.get("parent_run_id") is not None
        ):
            raise RuntimeError("parent submission identity is incomplete")
        await self._poll_run(
            parent_run_id,
            headers=headers,
            parent_run_id=None,
            campaign_id=submission.campaign_id,
            label="parent run",
        )
        children = await self._poll_children(parent_run_id, headers=headers)
        child_ids: list[str] = []
        child_threads: list[str] = []
        for child in children:
            child_id = self._string(child, "run_id", "child lineage")
            thread_id = self._string(child, "thread_id", "child lineage")
            if child.get("parent_run_id") != parent_run_id:
                raise RuntimeError("child lineage does not point to the real parent")
            if child.get("campaign_id") != submission.campaign_id:
                raise RuntimeError("child lineage campaign identity changed")
            child_ids.append(child_id)
            child_threads.append(thread_id)
        if (
            len(set(child_ids)) != ITEMS_PER_REPETITION
            or len(set(child_threads)) != ITEMS_PER_REPETITION
        ):
            raise RuntimeError("child lineage identities are missing or duplicate")

        await self._validate_chain(parent_run_id, headers=headers)
        parent_evidence = await self._evidence(parent_run_id, headers=headers)
        self._validate_parent_evidence(parent_run_id, parent_evidence)

        public_children: list[_PublicChild] = []
        for child_id in child_ids:
            await self._poll_run(
                child_id,
                headers=headers,
                parent_run_id=parent_run_id,
                campaign_id=submission.campaign_id,
                label="child run",
            )
            await self._validate_chain(child_id, headers=headers)
            evidence = await self._evidence(child_id, headers=headers)
            run_cost = self._validate_child_evidence(child_id, evidence)
            public_children.append(
                _PublicChild(run_id=child_id, evidence=evidence, run_cost_usd=run_cost)
            )
        return {"parent_run_id": parent_run_id, "children": tuple(public_children)}

    async def _poll_run(
        self,
        run_id: str,
        *,
        headers: Mapping[str, str],
        parent_run_id: str | None,
        campaign_id: str | None,
        label: str,
    ) -> Mapping[str, object]:
        for attempt in range(self._max_poll_attempts):
            value = await self._request_object(
                "GET",
                f"/v1/runs/{quote(run_id, safe='')}",
                headers=headers,
                payload=None,
                expected=200,
                label=label,
            )
            if value.get("run_id") != run_id or value.get("parent_run_id") != parent_run_id:
                raise RuntimeError(f"{label} identity or lineage changed")
            if campaign_id is not None and value.get("campaign_id") != campaign_id:
                raise RuntimeError(f"{label} campaign identity changed")
            status = value.get("status")
            if status == "succeeded":
                return value
            if status not in {"queued", "running"}:
                raise RuntimeError(f"{label} terminated without success")
            if attempt + 1 < self._max_poll_attempts:
                await self._sleeper(self._poll_interval_seconds)
        raise TimeoutError(f"{label} did not reach a terminal success state")

    async def _poll_children(
        self, parent_run_id: str, *, headers: Mapping[str, str]
    ) -> tuple[Mapping[str, object], ...]:
        for attempt in range(self._max_poll_attempts):
            raw = await self._request_json(
                "GET",
                f"/v1/runs/{quote(parent_run_id, safe='')}/children",
                headers=headers,
                payload=None,
                expected=200,
                label="child lineage",
            )
            if not isinstance(raw, list):
                raise RuntimeError("child lineage response is not a JSON array")
            if len(raw) == ITEMS_PER_REPETITION:
                if not all(isinstance(item, Mapping) for item in raw):
                    raise RuntimeError("child lineage response contains an invalid item")
                return tuple(raw)
            if len(raw) > ITEMS_PER_REPETITION:
                raise RuntimeError("child lineage contains more than eight runs")
            if attempt + 1 < self._max_poll_attempts:
                await self._sleeper(self._poll_interval_seconds)
        raise TimeoutError("child lineage did not expose exactly eight runs")

    async def _validate_chain(self, run_id: str, *, headers: Mapping[str, str]) -> None:
        chain = await self._request_object(
            "POST",
            f"/v1/runs/{quote(run_id, safe='')}/verify-chain",
            headers=headers,
            payload={},
            expected=200,
            label="signed audit chain",
        )
        if (
            chain.get("verified") is not True
            or chain.get("signature_verified") is not True
            or chain.get("unsigned_record_count") != 0
            or not isinstance(chain.get("record_count"), int)
            or isinstance(chain.get("record_count"), bool)
            or int(chain["record_count"]) < 1
        ):
            raise RuntimeError("signed audit chain is incomplete")

    async def _evidence(self, run_id: str, *, headers: Mapping[str, str]) -> Mapping[str, object]:
        return await self._request_object(
            "GET",
            f"/v1/runs/{quote(run_id, safe='')}/evidence",
            headers=headers,
            payload=None,
            expected=200,
            label="run evidence",
        )

    @staticmethod
    def _validate_parent_evidence(run_id: str, evidence: Mapping[str, object]) -> None:
        run = evidence.get("run")
        audits = evidence.get("audits")
        summary = evidence.get("summary")
        try:
            total_cost = (
                Decimal(str(summary.get("total_cost_usd")))
                if isinstance(summary, Mapping)
                else Decimal("NaN")
            )
        except (InvalidOperation, TypeError, ValueError):
            raise RuntimeError("parent run evidence has invalid cost") from None
        if (
            not isinstance(run, Mapping)
            or run.get("run_id") != run_id
            or not isinstance(audits, list)
            or not audits
            or not isinstance(summary, Mapping)
            or summary.get("priced_call_count") != 0
            or summary.get("cost_event_count") != 0
            or not total_cost.is_finite()
            or total_cost != 0
            or summary.get("cost_identity_state") != "not_applicable_no_priced_call"
            or summary.get("reconciliation_state") != "reconciled_zero_activity"
        ):
            raise RuntimeError("parent run evidence is incomplete or double-counted")

    @staticmethod
    def _validate_child_evidence(run_id: str, evidence: Mapping[str, object]) -> Decimal:
        run = evidence.get("run")
        audits = evidence.get("audits")
        summary = evidence.get("summary")
        if (
            not isinstance(run, Mapping)
            or run.get("run_id") != run_id
            or not isinstance(audits, list)
            or not audits
            or not isinstance(summary, Mapping)
            or summary.get("priced_call_count") != 1
            or summary.get("cost_event_count") != 1
            or summary.get("cost_identity_state") != "correlated"
            or summary.get("reconciliation_state") != "reconciled"
        ):
            raise RuntimeError("child economics evidence is incomplete")
        try:
            cost = Decimal(str(summary["total_cost_usd"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise RuntimeError("child economics evidence has invalid cost") from None
        _money(cost, "child run cost")
        return cost

    def _build_observation(
        self,
        *,
        submission: PlannedBatchSubmission,
        identity: BatchCollectionIdentity,
        collected: CollectedParentReconciliation,
        public_children: tuple[_PublicChild, ...],
    ) -> ParentBatchObservation:
        if (
            collected.campaign_id != identity.campaign_id
            or collected.repetition != identity.repetition
            or collected.parent_run_id != identity.parent_run_id
        ):
            raise RuntimeError("authoritative parent identity does not match public lineage")
        if (
            collected.configured_concurrency != submission.concurrency
            or collected.observed_peak_concurrency != submission.concurrency
        ):
            raise RuntimeError("authoritative concurrency is not exactly four")
        if len(collected.children) != ITEMS_PER_REPETITION or {
            item.item_index for item in collected.children
        } != set(range(ITEMS_PER_REPETITION)):
            raise RuntimeError("authoritative child item indexes are incomplete")
        public_by_run = {item.run_id: item for item in public_children}
        if {item.child_run_id for item in collected.children} != set(public_by_run):
            raise RuntimeError("authoritative child identity does not match public lineage")

        observations: list[BatchEconomicsObservation] = []
        regulus_ids: list[str] = []
        reserved_total = Decimal(0)
        for item in sorted(collected.children, key=lambda child: child.item_index):
            public = public_by_run[item.child_run_id]
            self._validate_collected_child(item, public)
            reserved_total += item.reserved_max_cost_usd
            regulus_ids.append(item.regulus_execution_event_id)
            observations.append(
                BatchEconomicsObservation(
                    campaign_id=submission.campaign_id,
                    repetition=submission.repetition,
                    item_index=item.item_index,
                    parent_run_id=identity.parent_run_id,
                    child_run_id=item.child_run_id,
                    operation_id=item.operation_id,
                    provider_request_id=item.provider_request_id,
                    audit_event_id=item.audit_event_id,
                    cost_event_ids=(item.cost_event_id,),
                    reservation_id=item.reservation_id,
                    reservation_operation_id=item.reservation_operation_id,
                    reservation_status=item.reservation_status,
                    reserved_max_cost_usd=item.reserved_max_cost_usd,
                    reservation_actual_cost_usd=item.reservation_actual_cost_usd,
                    reservation_released_cost_usd=item.reservation_released_cost_usd,
                    reservation_cleanup_status=item.reservation_cleanup_status,
                    cache_hit=item.cache_hit,
                    audit_cost_usd=item.audit_cost_usd,
                    run_cost_usd=public.run_cost_usd,
                    local_cost_usd=item.local_cost_usd,
                    economics_cost_usd=item.economics_cost_usd,
                    audit_signed=True,
                    audit_chain_verified=True,
                    parent_child_linked=True,
                )
            )
        if len(set(regulus_ids)) != ITEMS_PER_REPETITION:
            raise RuntimeError("Regulus execution identities are missing or duplicate")
        if reserved_total > submission.per_run_cap_usd:
            raise RuntimeError("authoritative reservations exceed the per-run cap")
        if collected.campaign_spend_after_usd > submission.campaign_cap_usd:
            raise RuntimeError("authoritative campaign spend exceeds the campaign cap")

        totals = {
            "audit": sum((item.audit_cost_usd for item in observations), Decimal(0)),
            "run": sum((item.run_cost_usd for item in observations), Decimal(0)),
            "local": sum((item.local_cost_usd for item in observations), Decimal(0)),
            "economics": sum((item.economics_cost_usd for item in observations), Decimal(0)),
        }
        if any(_outside_tolerance(totals["audit"], value) for value in totals.values()):
            raise RuntimeError("authoritative parent economics totals do not reconcile")
        return ParentBatchObservation(
            campaign_id=submission.campaign_id,
            repetition=submission.repetition,
            parent_run_id=identity.parent_run_id,
            status="succeeded",
            configured_concurrency=collected.configured_concurrency,
            observed_peak_concurrency=collected.observed_peak_concurrency,
            campaign_spend_after_usd=collected.campaign_spend_after_usd,
            audit_cost_usd=totals["audit"],
            run_cost_usd=totals["run"],
            local_cost_usd=totals["local"],
            economics_cost_usd=totals["economics"],
            audit_signed=True,
            audit_chain_verified=True,
            children=tuple(observations),
        )

    @staticmethod
    def _validate_collected_child(item: CollectedChildReconciliation, public: _PublicChild) -> None:
        if item.cache_hit:
            raise RuntimeError("live batch child unexpectedly used a cache hit")
        if (
            item.reservation_operation_id != item.operation_id
            or item.reservation_status != "committed"
        ):
            raise RuntimeError("authoritative reservation identity is not committed")
        if item.reservation_cleanup_status != "complete":
            raise RuntimeError("authoritative reservation cleanup is incomplete")
        if _outside_tolerance(item.reservation_actual_cost_usd, item.audit_cost_usd):
            raise RuntimeError("authoritative reservation actual cost does not reconcile")
        if item.reservation_actual_cost_usd > item.reserved_max_cost_usd:
            raise RuntimeError("authoritative call exceeds its reservation")
        if _outside_tolerance(
            item.reserved_max_cost_usd - item.reservation_actual_cost_usd,
            item.reservation_released_cost_usd,
        ):
            raise RuntimeError("authoritative reservation remainder is not released")
        if any(
            _outside_tolerance(item.audit_cost_usd, value)
            for value in (
                item.local_cost_usd,
                item.economics_cost_usd,
                public.run_cost_usd,
            )
        ):
            raise RuntimeError("authoritative child economics totals do not reconcile")
        audits = public.evidence.get("audits")
        if not isinstance(audits, list):
            raise RuntimeError("public child audit evidence is missing")
        matches = [
            audit
            for audit in audits
            if isinstance(audit, Mapping)
            and audit.get("audit_id") == item.audit_event_id
            and audit.get("run_id") == item.child_run_id
            and audit.get("cost_event_id") == item.cost_event_id
        ]
        if len(matches) != 1:
            raise RuntimeError("authoritative audit identity is absent from public evidence")
        try:
            public_audit_value = (
                matches[0].get("estimated_cost_usd")
                if matches[0].get("cost_measurement") == "estimated"
                else matches[0].get("cost_usd")
            )
            public_audit_cost = Decimal(str(public_audit_value))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            raise RuntimeError("public audit cost is invalid") from None
        if _outside_tolerance(public_audit_cost, item.audit_cost_usd):
            raise RuntimeError("authoritative audit cost differs from public evidence")

    async def _request_object(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None,
        expected: int,
        label: str,
    ) -> Mapping[str, object]:
        value = await self._request_json(
            method,
            path,
            headers=headers,
            payload=payload,
            expected=expected,
            label=label,
        )
        if not isinstance(value, Mapping):
            raise RuntimeError(f"{label} response is not a JSON object")
        return value

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None,
        expected: int,
        label: str,
    ) -> object:
        try:
            response = await self._http.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=payload,
                timeout=self._timeout_seconds,
            )
        except Exception:
            raise RuntimeError(f"{label} request failed") from None
        if response.status_code != expected:
            raise RuntimeError(f"{label} returned HTTP {response.status_code}")
        try:
            return response.json()
        except Exception:
            raise RuntimeError(f"{label} returned malformed JSON") from None

    @staticmethod
    def _string(value: Mapping[str, object], field: str, label: str) -> str:
        observed = value.get(field)
        if not isinstance(observed, str):
            raise RuntimeError(f"{label} is missing {field}")
        try:
            _opaque(observed, field)
        except ValueError:
            raise RuntimeError(f"{label} has invalid {field}") from None
        return observed


__all__ = [
    "ARM_PHRASE",
    "AuthoritativeReconciliationCollector",
    "BatchCollectionIdentity",
    "BatchProviderServiceAdapter",
    "CollectedChildReconciliation",
    "CollectedParentReconciliation",
    "PRODUCT_API_GAPS",
]
