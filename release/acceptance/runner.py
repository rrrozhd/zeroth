"""Fail-closed execution engine for the deployed acceptance contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol

from .config import ResolvedAcceptanceConfig
from .lifecycle import HttpLifecycleController, LifecycleController
from .models import (
    REQUIRED_SCENARIOS,
    AcceptanceContract,
    AcceptanceReport,
    AcceptanceStep,
    ScenarioResult,
    ScenarioStatus,
    StepObservation,
)
from .transport import TransportError


class AcceptanceTransportLike(Protocol):
    """Protocol seam used by the runner and deterministic tests."""

    async def request(
        self, role: str | None, method: str, path: str, *, json_body: Any | None = None
    ) -> Any: ...

    async def websocket_events(
        self, role: str, path: str, payload: Any, *, max_events: int
    ) -> list[Any]: ...


def _subset(expected: Any, actual: Any, path: str = "body") -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f"{path} expected an object, got {type(actual).__name__}")
        for key, value in expected.items():
            if key not in actual:
                raise AssertionError(f"{path}.{key} is missing")
            _subset(value, actual[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        if expected != actual:
            raise AssertionError(f"{path} expected {expected!r}, got {actual!r}")
        return
    if expected != actual:
        raise AssertionError(f"{path} expected {expected!r}, got {actual!r}")


def _read(value: Any, dotted_path: str) -> Any:
    current = value
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise AssertionError(f"capture path {dotted_path!r} is missing")
        current = current[part]
    return current


class AcceptanceRunner:
    """Run every mandatory scenario and cleanup operation without silent skips."""

    def __init__(
        self,
        config: ResolvedAcceptanceConfig,
        contract: AcceptanceContract,
        transport: AcceptanceTransportLike,
        lifecycle: LifecycleController | None = None,
    ) -> None:
        self.config = config
        self.contract = contract
        self.transport = transport
        if lifecycle is None:
            if config.lifecycle is None:
                raise ValueError("a lifecycle controller or lifecycle endpoints are required")
            lifecycle = HttpLifecycleController(transport, config.lifecycle)
        self.lifecycle = lifecycle
        self._context: dict[str, Any] = {
            "namespace": config.namespace,
            "tenant_id": config.tenant_id,
            "deployment_ref": config.deployment_ref,
            "candidate_digest": config.candidate_digest,
        }
        self._observed_compatibility: dict[str, Any] | None = None
        self._owned_resources: set[str] = set()

    def _format(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return value.format_map(self._context)
            except KeyError as error:
                raise AssertionError(f"unresolved acceptance variable {error.args[0]!r}") from error
        if isinstance(value, dict):
            return {key: self._format(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._format(item) for item in value]
        return value

    def _assert_count(self, step: AcceptanceStep, body: Any, path: str) -> None:
        """Assert how many records in a response collection match a pattern.

        Side-effect claims need a count, not a flag. "The approval-gated node ran zero
        times, then exactly once" is only checkable black-box by counting the records
        the deployment itself published for that node.
        """
        collection = _read(body, str(step.count_path))
        if not isinstance(collection, list):
            raise AssertionError(f"{path}.{step.count_path} is not a collection")
        pattern = self._format(step.count_where)
        matched = 0
        for item in collection:
            try:
                _subset(pattern, item)
            except AssertionError:
                continue
            matched += 1
        if matched != step.expected_count:
            raise AssertionError(
                f"{path}.{step.count_path} matching {pattern!r} expected "
                f"{step.expected_count} record(s), got {matched}"
            )

    async def _step(self, step: AcceptanceStep) -> StepObservation:
        """Run one step, retrying only those the contract marks as eventually true.

        A deployed run settles asynchronously, so a single GET proves nothing about a
        terminal status. Polling is opt-in per step and bounded by the configured
        deadline, and the last failure is what surfaces when the deadline passes — a
        bare timeout would hide which assertion never became true.
        """
        if not step.poll:
            return await self._execute_step(step)
        deadline = asyncio.get_running_loop().time() + self.config.poll_deadline_seconds
        attempts = 0
        while True:
            attempts += 1
            try:
                return await self._execute_step(step)
            except (AssertionError, TransportError) as error:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"{error} (still false after {attempts} attempt(s) in "
                        f"{self.config.poll_deadline_seconds:g}s)"
                    ) from error
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _execute_step(self, step: AcceptanceStep) -> StepObservation:
        if step.protocol == "lifecycle":
            return await self._execute_lifecycle(step)
        path = self._format(step.path)
        if step.method == "DELETE":
            resource_id = self._format(step.resource_id)
            if resource_id not in self._owned_resources:
                self.config.require_owned(resource_id)
        if step.protocol == "http":
            return await self._execute_http(step, path)
        return await self._execute_websocket(step, path)

    async def _execute_lifecycle(self, step: AcceptanceStep) -> StepObservation:
        if step.operation == "restart":
            await self.lifecycle.restart()
        else:
            await self.lifecycle.shutdown()
        return StepObservation(protocol="lifecycle", path=step.operation or "")

    async def _execute_http(self, step: AcceptanceStep, path: str) -> StepObservation:
        role = None if step.role == "anonymous" else step.role
        if step.expect_unreachable:
            return await self._expect_unreachable(step, path, role)
        response = await self.transport.request(
            role,
            step.method or "GET",
            path,
            json_body=self._format(step.payload),
        )
        if response.status_code != step.expected_status:
            raise AssertionError(
                f"{path} expected HTTP {step.expected_status}, got {response.status_code}: "
                f"{response.body!r}"
            )
        if step.expected_json:
            _subset(self._format(step.expected_json), response.body)
        if step.count_path is not None:
            self._assert_count(step, response.body, path)
        if step.require_correlation and not response.correlation_id:
            raise AssertionError(f"{path} omitted X-Correlation-ID")
        self._apply_captures(step, response.body, path)
        return StepObservation(
            protocol="http",
            path=path,
            status_code=response.status_code,
            correlation_id=response.correlation_id,
        )

    async def _expect_unreachable(
        self, step: AcceptanceStep, path: str, role: str | None
    ) -> StepObservation:
        try:
            response = await self.transport.request(role, step.method or "GET", path)
        except TransportError:
            return StepObservation(protocol="http", path=path)
        raise AssertionError(
            f"{path} is still served (HTTP {response.status_code}) after the candidate "
            "was withdrawn"
        )

    def _apply_captures(self, step: AcceptanceStep, body: Any, path: str) -> None:
        for name, dotted_path in step.capture.items():
            self._context[name] = _read(body, dotted_path)
        for name, dotted_path in step.owned_capture.items():
            owned = _read(body, dotted_path)
            if not isinstance(owned, str) or not owned:
                raise AssertionError(f"owned capture {name!r} is not a resource identifier")
            self._context[name] = owned
            self._owned_resources.add(owned)
        if path.endswith("compatibility") and isinstance(body, dict):
            self._observed_compatibility = body

    async def _execute_websocket(self, step: AcceptanceStep, path: str) -> StepObservation:
        events = await self.transport.websocket_events(
            step.role,
            path,
            self._format(step.payload),
            max_events=step.max_events or 1,
        )
        names = [event.get("event") for event in events if isinstance(event, dict)]
        if names != step.ordered_events:
            raise AssertionError(
                f"{path} expected ordered events {step.ordered_events!r}, got {names!r}"
            )
        # Arrival order on one WebSocket connection *is* causal order, so the check
        # above already carries the ordering claim. An explicit sequence number is an
        # extra guarantee only some streams publish: the Zeroth gateway bridges
        # upstream frames without adding one, so demanding it unconditionally would
        # make this scenario satisfiable by a synthetic server and by nothing else.
        # Where a stream does number its frames, hold it to them.
        sequences = [
            event["sequence"] for event in events if isinstance(event, dict) and "sequence" in event
        ]
        if sequences:
            if len(sequences) != len(events):
                raise AssertionError(f"{path} numbered only some of its events")
            if not all(isinstance(value, int) for value in sequences):
                raise AssertionError(f"{path} event sequence numbers are not integers")
            if sequences != sorted(set(sequences)):
                raise AssertionError(f"{path} events are not uniquely causally ordered")
        return StepObservation(protocol="websocket", path=path, event_count=len(events))

    async def _scenario(self, name: str, steps: list[AcceptanceStep]) -> ScenarioResult:
        observations: list[StepObservation] = []
        try:
            for step in steps:
                observations.append(await self._step(step))
        except Exception as error:  # noqa: BLE001 - failures become stable evidence
            return ScenarioResult(
                name=name,
                status=ScenarioStatus.FAILED,
                detail=str(error),
                observations=observations,
            )
        return ScenarioResult(
            name=name,
            status=ScenarioStatus.PASSED,
            detail=f"passed {len(observations)} step(s)",
            observations=observations,
        )

    async def run(self) -> AcceptanceReport:
        """Execute the complete contract and always attempt bounded cleanup."""
        started = datetime.now(UTC)
        scenarios = [
            await self._scenario(name, self.contract.scenarios[name].steps)
            for name in REQUIRED_SCENARIOS
            if name != "shutdown"
        ]
        cleanup = [
            await self._scenario(f"cleanup-{index + 1}", [step])
            for index, step in enumerate(self.contract.cleanup)
        ]
        scenarios.append(
            await self._scenario("shutdown", self.contract.scenarios["shutdown"].steps)
        )
        status = (
            ScenarioStatus.PASSED
            if all(result.status is ScenarioStatus.PASSED for result in [*scenarios, *cleanup])
            else ScenarioStatus.FAILED
        )
        return AcceptanceReport(
            status=status,
            target_origin=self.config.base_url,
            tenant_id=self.config.tenant_id,
            namespace=self.config.namespace,
            deployment_ref=self.config.deployment_ref,
            candidate_digest=self.config.candidate_digest,
            image_identity=self.config.candidate_identity["image"],
            observed_compatibility=self._observed_compatibility,
            started_at=started,
            finished_at=datetime.now(UTC),
            scenarios=scenarios,
            cleanup=cleanup,
        )
