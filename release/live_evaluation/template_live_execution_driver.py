"""One-shot live driver for the approved template-rendered execution fixture.

The driver owns only Zeroth service authentication.  It accepts no provider
credential input and delegates the single paid submission to the public run
API after validating both operator interlocks and the metadata-only readiness
attestation.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from release.live_evaluation.batch_provider_economics import ReadinessAttestation
from release.live_evaluation.live_provider_gate import (
    ProviderFreeWiring,
    ReadinessBlocked,
    _object,
    _parse_wiring,
)
from release.live_evaluation.template_cost_identity import PersistedCostIdentityReader
from release.live_evaluation.template_live_rendered_execution import (
    ARM_ENVIRONMENT_VARIABLE,
    CAMPAIGN_CEILING_USD,
    CRITERION_ID,
    RUN_CEILING_USD,
    LiveTemplateHarness,
    LiveTemplateObservation,
)
from release.live_evaluation.template_provisioning_cli import LoopbackServiceRequest

ARM_PHRASE = "AUTHORIZE_LIVE_TEMPLATE_PROVIDER_SPEND"
_CONTAINER_NAME = "zeroth-dev-backend-1"
_SECRET_SHAPED = (
    "authorization",
    "api_key",
    "api-key",
    "provider_key",
    "provider-key",
    "service_key",
    "service-key",
    "bearer ",
    "sk-proj-",
)


class DriverBlockedError(RuntimeError):
    """Stable fail-closed reason safe to print to an operator."""


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage()
        self.exit(2, f"{self.prog}: error: invalid command arguments\n")


CommandRunner = Callable[[tuple[str, ...]], tuple[int, str, str]]


def _command(argv: tuple[str, ...]) -> tuple[int, str, str]:
    completed = subprocess.run(  # noqa: S603 - fixed docker argv, no shell
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.returncode, completed.stdout, completed.stderr


class DockerBackendLifecycle:
    """Restart the fixed primary backend without rebuilding or changing its env."""

    def __init__(
        self,
        *,
        compose_file: Path,
        command_runner: CommandRunner = _command,
        wait: Callable[[float], None] = time.sleep,
        health_attempts: int = 120,
    ) -> None:
        if compose_file.is_symlink() or not compose_file.is_file():
            raise DriverBlockedError("compose-file-unavailable")
        if health_attempts < 1:
            raise ValueError("health_attempts must be positive")
        self._compose_file = compose_file.resolve(strict=True)
        self._command_runner = command_runner
        self._wait = wait
        self._health_attempts = health_attempts

    def instance_id(self) -> str:
        code, stdout, _stderr = self._command_runner(
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.StartedAt}}",
                _CONTAINER_NAME,
            )
        )
        identity = stdout.strip()
        if code != 0 or not identity or any(character.isspace() for character in identity):
            raise DriverBlockedError("backend-instance-unavailable")
        return identity

    def restart(self) -> None:
        code, _stdout, _stderr = self._command_runner(
            (
                "docker",
                "compose",
                "-f",
                str(self._compose_file),
                "restart",
                "backend",
            )
        )
        if code != 0:
            raise DriverBlockedError("backend-restart-failed")
        health_command = (
            "docker",
            "inspect",
            "--format",
            "{{.State.Health.Status}}",
            _CONTAINER_NAME,
        )
        for attempt in range(self._health_attempts):
            health_code, stdout, _stderr = self._command_runner(health_command)
            if health_code == 0 and stdout.strip() == "healthy":
                return
            if attempt + 1 < self._health_attempts:
                self._wait(0.25)
        raise DriverBlockedError("backend-restart-unhealthy")


def _read_readiness(path: Path) -> ReadinessAttestation:
    try:
        return ReadinessAttestation.from_mapping(
            _object(path, "provider_readiness_attestation_invalid")
        )
    except (ReadinessBlocked, TypeError, ValueError):
        raise DriverBlockedError("provider-readiness-invalid") from None


def _validate_readiness(readiness: ReadinessAttestation, *, wiring: ProviderFreeWiring) -> None:
    expected_identity = (
        wiring.template_config.tenant_id,
        wiring.template_config.tenant_id,
        "llm.openai",
    )
    observed_identity = (
        readiness.campaign_id,
        readiness.tenant_id,
        readiness.logical_secret_ref,
    )
    identities = (
        readiness.operation_id,
        readiness.run_id,
        readiness.audit_event_id,
        readiness.cost_event_id,
    )
    if (
        observed_identity != expected_identity
        or readiness.installed is not True
        or readiness.provider_probe_reconciled is not True
        or readiness.audit_signed is not True
        or len(set(identities)) != len(identities)
        or readiness.measured_cost_usd > RUN_CEILING_USD
        or readiness.campaign_spend_before_usd > CAMPAIGN_CEILING_USD
        or readiness.campaign_spend_before_usd + RUN_CEILING_USD > CAMPAIGN_CEILING_USD
    ):
        raise DriverBlockedError("provider-readiness-invalid")


def execute_approved_template(
    *,
    wiring: ProviderFreeWiring,
    readiness: ReadinessAttestation,
    arm_phrase: str,
    environment: Mapping[str, str],
    service_api_key_file: Path | None = None,
    request_factory: Callable[..., Any] = LoopbackServiceRequest,
    cost_reader_factory: Callable[..., Any] = PersistedCostIdentityReader,
    lifecycle_factory: Callable[[], Any] | None = None,
) -> LiveTemplateObservation:
    """Execute exactly one approved template run and return its sanitized proof."""
    if arm_phrase != ARM_PHRASE or environment.get(ARM_ENVIRONMENT_VARIABLE) != CRITERION_ID:
        raise DriverBlockedError("live-template-not-armed")
    _validate_readiness(readiness, wiring=wiring)
    if service_api_key_file is None:
        raise DriverBlockedError("service-key-file-unavailable")

    repository_root = Path(__file__).resolve().parents[2]
    make_lifecycle = lifecycle_factory or (
        lambda: DockerBackendLifecycle(compose_file=repository_root / "compose.dev.yml")
    )
    request = request_factory(
        base_url=wiring.service_base_url,
        service_api_key_file=service_api_key_file,
    )
    submissions = 0

    def one_shot_request(method: str, path: str, payload: dict[str, Any] | None) -> Any:
        nonlocal submissions
        if method == "POST" and path == "/v1/runs":
            submissions += 1
            if submissions > 1:
                raise DriverBlockedError("template-run-limit-exceeded")
        return request(method, path, payload)

    try:
        cost_reader = cost_reader_factory(
            database=wiring.econ_database,
            tenant_id=wiring.template_config.tenant_id,
            campaign_id=wiring.template_config.tenant_id,
            expected_provider="openai",
        )
        harness = LiveTemplateHarness(
            config=wiring.template_config,
            request=one_shot_request,
            secret_reference_available=lambda reference, tenant: (
                reference == readiness.logical_secret_ref
                and tenant == readiness.tenant_id
                and readiness.installed
            ),
            cost_identity=cost_reader,
            environment=environment,
        )
        observation = harness.execute_service(
            armed=True,
            fixture=wiring.template_fixture,
            lifecycle=make_lifecycle(),
        )
        if submissions != 1:
            raise DriverBlockedError("template-run-count-invalid")
        return observation
    finally:
        close = getattr(request, "close", None)
        if callable(close):
            close()


def write_observation_exclusive(path: Path, observation: LiveTemplateObservation) -> None:
    payload = json.dumps(observation.to_dict(), sort_keys=True, indent=2) + "\n"
    lowered = payload.lower()
    if any(shape in lowered for shape in _SECRET_SHAPED):
        raise DriverBlockedError("observation-secret-shaped")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def _prepare_output_target(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise DriverBlockedError("output-already-exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DriverBlockedError("output-parent-unavailable") from None
    cursor = path.parent.absolute()
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise DriverBlockedError("output-parent-unsafe")
        cursor = cursor.parent
    if not path.parent.is_dir() or not os.access(path.parent, os.W_OK):
        raise DriverBlockedError("output-parent-unavailable")


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="template-live-execution-driver")
    parser.add_argument("--wiring-config", required=True, type=Path)
    parser.add_argument("--readiness-attestation", required=True, type=Path)
    parser.add_argument("--service-api-key-file", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--arm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _prepare_output_target(args.output_json)
        wiring = _parse_wiring(_object(args.wiring_config, "wiring_configuration_invalid"))
        readiness = _read_readiness(args.readiness_attestation)
        observation = execute_approved_template(
            wiring=wiring,
            readiness=readiness,
            arm_phrase=args.arm,
            environment=os.environ,
            service_api_key_file=args.service_api_key_file,
        )
        write_observation_exclusive(args.output_json, observation)
    except DriverBlockedError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    except (ReadinessBlocked, ValueError):
        print(json.dumps({"status": "blocked", "reason": "approved-input-invalid"}, sort_keys=True))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "execution-failed"}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "observed",
                "criterion_id": observation.criterion_id,
                "run_id": observation.run_id,
                "audit_id": observation.audit_id,
                "cost_event_id": observation.cost_event_id,
                "provider_request_id": observation.provider_request_id,
                "provider_calls_performed": 1,
                "output_json": str(args.output_json),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARM_PHRASE",
    "DockerBackendLifecycle",
    "DriverBlockedError",
    "build_parser",
    "execute_approved_template",
    "main",
    "write_observation_exclusive",
]
