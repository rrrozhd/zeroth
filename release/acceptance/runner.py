"""Fail-closed execution engine for the deployed acceptance contract."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from .config import ResolvedAcceptanceConfig
from .models import (
    REQUIRED_SCENARIOS,
    AcceptanceContract,
    AcceptanceReport,
    AcceptanceStep,
    ScenarioResult,
    ScenarioStatus,
    StepObservation,
)


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
    ) -> None:
        self.config = config
        self.contract = contract
        self.transport = transport
        self._context: dict[str, Any] = {
            "namespace": config.namespace,
            "tenant_id": config.tenant_id,
            "deployment_ref": config.deployment_ref,
            "candidate_digest": config.candidate_digest,
            "restart_url": config.lifecycle.restart_url,
            "shutdown_url": config.lifecycle.shutdown_url,
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

    async def _step(self, step: AcceptanceStep) -> StepObservation:
        path = self._format(step.path)
        if step.method == "DELETE":
            resource_id = self._format(step.resource_id)
            if resource_id not in self._owned_resources:
                self.config.require_owned(resource_id)
        if step.protocol == "http":
            role = None if step.role == "anonymous" else step.role
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
            if step.require_correlation and not response.correlation_id:
                raise AssertionError(f"{path} omitted X-Correlation-ID")
            for name, dotted_path in step.capture.items():
                self._context[name] = _read(response.body, dotted_path)
            for name, dotted_path in step.owned_capture.items():
                owned = _read(response.body, dotted_path)
                if not isinstance(owned, str) or not owned:
                    raise AssertionError(f"owned capture {name!r} is not a resource identifier")
                self._context[name] = owned
                self._owned_resources.add(owned)
            if path.endswith("compatibility") and isinstance(response.body, dict):
                self._observed_compatibility = response.body
            return StepObservation(
                protocol="http",
                path=path,
                status_code=response.status_code,
                correlation_id=response.correlation_id,
            )

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
        sequences = [event.get("sequence") for event in events if isinstance(event, dict)]
        if not all(isinstance(value, int) for value in sequences) or sequences != sorted(
            set(sequences)
        ):
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
