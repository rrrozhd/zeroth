"""Prepare and serve the provider-free resilient-HTTP Studio fixture.

The scenario peer shares the backend container's network namespace and binds
only to its loopback interface.  The controller always restores the exact
pre-journey serving identity; it never seals evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .evidence import EvidenceStore
from .provider_free_composed import (
    HttpFixtureClient,
    Request,
    _post,
    _publish_deploy_workflow,
)
from .provider_free_composed import _object as response_object


@dataclass(frozen=True, slots=True)
class ServingIdentity:
    deployment_ref: str
    deployment_version: int
    graph_version_ref: str


@dataclass(frozen=True, slots=True)
class ResilientHttpFixture:
    schema_version: int
    fixture_id: str
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    provider_calls_performed: int = 0
    restart_required: bool = True


@dataclass(frozen=True, slots=True)
class BrowserJourneyResult:
    returncode: int
    restore: dict[str, object]


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    x: int,
    y: int = 0,
    config: Mapping[str, object] | None = None,
    input_contract_ref: str,
    output_contract_ref: str,
) -> dict[str, object]:
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": y},
        "data": {
            "label": label,
            "config": dict(config or {}),
            "input_contract_ref": input_contract_ref,
            "output_contract_ref": output_contract_ref,
        },
    }


def provision_resilient_http_fixture(*, request: Request, fixture_id: str) -> ResilientHttpFixture:
    """Publish a three-route private-GET workflow through the public APIs."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    prefix = f"contract://provider-free-resilient-http-{fixture_id}"
    input_contract = f"{prefix}.input"
    output_contract = f"{prefix}.output"
    schemas = (
        (
            input_contract,
            {
                "type": "object",
                "additionalProperties": True,
                "required": ["scenario"],
                "properties": {
                    "scenario": {"type": "string", "enum": ["retry", "timeout", "circuit"]},
                    "zeroth_if": {"type": "object"},
                },
            },
        ),
        (
            output_contract,
            {
                "type": "object",
                "additionalProperties": True,
                "required": ["http_response"],
                "properties": {"http_response": {"type": "object"}},
            },
        ),
    )
    for name, schema in schemas:
        created = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {
                    "campaign_slice": "resilient-http-live",
                    "provider_calls_performed": 0,
                },
            },
            expected=201,
            label=f"create {name}",
        )
        if created.get("name") != name or created.get("version") != 1:
            raise RuntimeError("resilient-HTTP contract is not an immutable v1 fixture")

    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Provider-free resilient HTTP {fixture_id}"},
        expected=201,
        label="create resilient-HTTP workflow",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError("resilient-HTTP workflow identity is missing")

    common_http = {
        "method": "GET",
        "retryable_status_codes": [503],
        "max_response_bytes": 4096,
    }
    nodes = [
        _node(
            "request",
            "entrypoint",
            label="Scenario request",
            x=0,
            input_contract_ref=input_contract,
            output_contract_ref=input_contract,
        ),
        _node(
            "route-retry",
            "if",
            label="Retry scenario?",
            x=300,
            config={"expression": "payload.scenario == 'retry'"},
            input_contract_ref=input_contract,
            output_contract_ref=input_contract,
        ),
        _node(
            "http-retry",
            "http_request",
            label="Retry then succeed",
            x=640,
            y=-220,
            config={
                **common_http,
                "url": "http://127.0.0.1:8787/scenario/retry-then-success",
                "timeout_seconds": 1.0,
                "max_retries": 2,
            },
            input_contract_ref=input_contract,
            output_contract_ref=output_contract,
        ),
        _node(
            "route-timeout",
            "if",
            label="Timeout scenario?",
            x=640,
            y=80,
            config={"expression": "payload.scenario == 'timeout'"},
            input_contract_ref=input_contract,
            output_contract_ref=input_contract,
        ),
        _node(
            "http-timeout",
            "http_request",
            label="Timeout with exhaustion",
            x=980,
            y=20,
            config={
                **common_http,
                "url": "http://127.0.0.1:8787/scenario/timeout",
                "timeout_seconds": 0.05,
                "max_retries": 1,
            },
            input_contract_ref=input_contract,
            output_contract_ref=output_contract,
        ),
        _node(
            "http-circuit",
            "http_request",
            label="Circuit and recovery",
            x=980,
            y=260,
            config={
                **common_http,
                "url": "http://127.0.0.1:8787/scenario/circuit",
                "timeout_seconds": 1.0,
                "max_retries": 0,
            },
            input_contract_ref=input_contract,
            output_contract_ref=output_contract,
        ),
    ]
    edges = [
        {"id": "request-route", "source": "request", "target": "route-retry"},
        {
            "id": "retry-true",
            "source": "route-retry",
            "target": "http-retry",
            "source_handle": "true",
        },
        {
            "id": "retry-false",
            "source": "route-retry",
            "target": "route-timeout",
            "source_handle": "false",
        },
        {
            "id": "timeout-true",
            "source": "route-timeout",
            "target": "http-timeout",
            "source_handle": "true",
        },
        {
            "id": "timeout-false",
            "source": "route-timeout",
            "target": "http-circuit",
            "source_handle": "false",
        },
    ]
    saved = response_object(
        request(
            "PUT",
            f"/api/studio/v1/workflows/{workflow_id}",
            {
                "entry_step": "request",
                "nodes": nodes,
                "edges": edges,
                "execution_settings": {
                    # The longest successful route visits four nodes; the
                    # runtime guard must allow the terminal transition after it.
                    "max_total_steps": 5,
                    "max_total_runtime_seconds": 15,
                    "max_visits_per_node": 1,
                    "default_timeout_seconds": 2,
                },
            },
        ),
        expected=200,
        label="save resilient-HTTP workflow",
    )
    if saved.get("id") not in (None, workflow_id):
        raise RuntimeError("saved resilient-HTTP workflow identity drifted")
    deployment_ref = f"provider-free-resilient-http-{fixture_id}"
    graph_ref, deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=workflow_id,
        deployment_ref=deployment_ref,
    )
    return ResilientHttpFixture(
        schema_version=1,
        fixture_id=fixture_id,
        workflow_id=workflow_id,
        graph_version_ref=graph_ref,
        deployment_ref=deployment_ref,
        deployment_version=deployment_version,
    )


class ResilientHttpDockerController:
    """Bounded backend/scenario switch with exact restoration."""

    def __init__(
        self,
        *,
        workspace: Path,
        compose_file: Path,
        override_file: Path,
        observe: Callable[[], ServingIdentity],
        command_runner: Any = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.compose_file = compose_file.resolve(strict=True)
        self.override_file = override_file.resolve(strict=True)
        if (
            self.compose_file.parent != self.workspace
            or self.override_file.parent != self.workspace
        ):
            raise ValueError("compose files must be workspace-owned")
        self.observe = observe
        self.command_runner = command_runner
        self.sleep = sleep

    def capture(self) -> ServingIdentity:
        return self.observe()

    def _run(self, argv: tuple[str, ...], *, deployment_ref: str | None = None) -> None:
        environment = dict(__import__("os").environ)
        if deployment_ref is not None:
            environment["ZEROTH_DEV_DEPLOYMENT_REF"] = deployment_ref
        completed = self.command_runner(
            argv,
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError("bounded resilient-HTTP Docker command failed")

    def _wait(self, expected: ServingIdentity) -> ServingIdentity:
        for _ in range(80):
            try:
                observed = self.observe()
            except Exception:
                observed = None
            if observed == expected:
                return expected
            self.sleep(0.25)
        raise RuntimeError("backend did not serve the exact resilient-HTTP identity")

    def serve(
        self,
        *,
        deployment_ref: str,
        deployment_version: int,
        graph_version_ref: str,
    ) -> ServingIdentity:
        expected = ServingIdentity(deployment_ref, deployment_version, graph_version_ref)
        self._run(
            (
                "docker",
                "compose",
                "-f",
                str(self.compose_file),
                "-f",
                str(self.override_file),
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "60",
                "--force-recreate",
                "backend",
            ),
            deployment_ref=deployment_ref,
        )
        self._run(
            (
                "docker",
                "compose",
                "-f",
                str(self.compose_file),
                "-f",
                str(self.override_file),
                "exec",
                "-T",
                "-d",
                "backend",
                "python",
                "-m",
                "release.live_evaluation.resilient_http_scenario_server",
                "--host",
                "127.0.0.1",
                "--port",
                "8787",
                "--retry-failures",
                "2",
                "--timeout-seconds",
                "0.25",
            )
        )
        return self._wait(expected)

    def restore(self, before: ServingIdentity) -> dict[str, object]:
        self._run(
            (
                "docker",
                "compose",
                "-f",
                str(self.compose_file),
                "up",
                "-d",
                "--force-recreate",
                "backend",
            ),
            deployment_ref=before.deployment_ref,
        )
        after = self._wait(before)
        return {"before": asdict(before), "after": asdict(after), "exact": after == before}


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def execute_browser_journey(
    *,
    controller: ResilientHttpDockerController,
    before: ServingIdentity,
    fixture: ResilientHttpFixture,
    source_root: Path,
    browser_environment: Mapping[str, str],
    command_runner: Any = subprocess.run,
    scenario_probe: Callable[[], bool],
    workspace: Path | None = None,
) -> BrowserJourneyResult:
    """Run Playwright under one finally-owned exact restoration boundary."""
    root = source_root.resolve(strict=True)
    checkout = (workspace or Path(__file__).resolve().parents[2]).resolve(strict=True)
    completed: Any | None = None
    pending_error: BaseException | None = None
    try:
        controller.serve(
            deployment_ref=fixture.deployment_ref,
            deployment_version=fixture.deployment_version,
            graph_version_ref=fixture.graph_version_ref,
        )
        for _ in range(120):
            if scenario_probe():
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("loopback resilient-HTTP scenario peer did not become ready")
        completed = command_runner(
            (
                "npx",
                "playwright",
                "test",
                "e2e/resilient-http-live.spec.ts",
                "--project=desktop-1440",
                "--project=webkit-1440",
            ),
            cwd=checkout / "frontend",
            env=dict(browser_environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=360,
        )
    except BaseException as exc:
        pending_error = exc
    finally:
        restore = controller.restore(before)
        _write_private(
            root / "runtime/d012-restore.json",
            json.dumps(restore, indent=2, sort_keys=True) + "\n",
        )
    if pending_error is not None:
        raise pending_error
    if completed is None:
        raise RuntimeError("Playwright did not return a result")
    _write_private(root / "commands/playwright.stdout.txt", completed.stdout)
    _write_private(root / "commands/playwright.stderr.txt", completed.stderr)
    _write_private(root / "commands/playwright.exit.txt", f"{completed.returncode}\n")
    return BrowserJourneyResult(returncode=completed.returncode, restore=restore)


def _identity_from_health(value: Mapping[str, object]) -> ServingIdentity:
    deployment_ref = value.get("deployment_ref")
    deployment_version = value.get("deployment_version")
    graph_version_ref = value.get("graph_version_ref")
    if (
        value.get("status") != "ok"
        or not isinstance(deployment_ref, str)
        or not deployment_ref
        or not isinstance(deployment_version, int)
        or isinstance(deployment_version, bool)
        or deployment_version < 1
        or not isinstance(graph_version_ref, str)
        or not graph_version_ref
    ):
        raise RuntimeError("service health lacks an exact serving identity")
    return ServingIdentity(deployment_ref, deployment_version, graph_version_ref)


def scenario_peer_ready(
    *,
    workspace: Path,
    compose_file: Path,
    override_file: Path,
    command_runner: Any = subprocess.run,
) -> bool:
    """Probe the peer from the only namespace allowed to reach it."""
    completed = command_runner(
        (
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "-f",
            str(override_file),
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            (
                "import urllib.request; "
                "assert urllib.request.urlopen("
                "'http://127.0.0.1:8787/health', timeout=1).status == 200"
            ),
        ),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    return completed.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8122")
    parser.add_argument("--frontend-base", default="http://127.0.0.1:3000")
    parser.add_argument("--scenario-base", default="http://127.0.0.1:8787")
    parser.add_argument("--tenant-id", default="evaluation-studio-v1")
    args = parser.parse_args()
    api_key = os.environ.get("ZEROTH_EVALUATION_API_KEY")
    if not api_key:
        raise RuntimeError("ZEROTH_EVALUATION_API_KEY is required")
    source_root = args.source_root.expanduser().resolve(strict=False)
    source_root.mkdir(parents=True, mode=0o700)
    os.chmod(source_root, 0o700)
    workspace = Path(__file__).resolve().parents[2]
    with httpx.Client(timeout=10.0, follow_redirects=False) as public_client:
        fixture_client = HttpFixtureClient(
            base_url=args.api_base,
            api_key=api_key,
            tenant_id=args.tenant_id,
            client=public_client,
        )

        def observe() -> ServingIdentity:
            response = public_client.get(f"{args.api_base}/health")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, Mapping):
                raise RuntimeError("health response is malformed")
            return _identity_from_health(value)

        before = observe()
        fixture = provision_resilient_http_fixture(
            request=fixture_client,
            fixture_id=args.fixture_id,
        )
        _write_private(
            source_root / "runtime/fixture.json",
            json.dumps(asdict(fixture), indent=2, sort_keys=True) + "\n",
        )
        controller = ResilientHttpDockerController(
            workspace=workspace,
            compose_file=workspace / "compose.dev.yml",
            override_file=workspace / "compose.resilient-http-live.yml",
            observe=observe,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PLAYWRIGHT_NO_SERVER": "1",
                "ZEROTH_EVALUATION_HTTP_LIVE": "1",
                "ZEROTH_EVALUATION_API_BASE": args.api_base,
                "ZEROTH_EVALUATION_BASE_URL": args.frontend_base,
                "ZEROTH_EVALUATION_BROWSER_ROOT": str(source_root / "browser"),
                "ZEROTH_EVALUATION_TENANT": args.tenant_id,
                "ZEROTH_EVALUATION_HTTP_WORKFLOW_ID": fixture.workflow_id,
                "ZEROTH_EVALUATION_HTTP_DEPLOYMENT_REF": fixture.deployment_ref,
                "ZEROTH_EVALUATION_HTTP_GRAPH_VERSION": fixture.graph_version_ref,
                "ZEROTH_EVALUATION_HTTP_SCENARIO_BASE": args.scenario_base,
                "ZEROTH_EVALUATION_HTTP_BREAKER_THRESHOLD": "1",
                "ZEROTH_EVALUATION_HTTP_BREAKER_RESET_MS": "31000",
                "ZEROTH_EVALUATION_CAMPAIGN_ID": "evaluation-studio-v1",
            }
        )

        def scenario_ready() -> bool:
            return scenario_peer_ready(
                workspace=workspace,
                compose_file=workspace / "compose.dev.yml",
                override_file=workspace / "compose.resilient-http-live.yml",
            )

        result = execute_browser_journey(
            controller=controller,
            before=before,
            fixture=fixture,
            source_root=source_root,
            browser_environment=environment,
            scenario_probe=scenario_ready,
            workspace=workspace,
        )
    EvidenceStore(source_root).scan_recursive()
    if result.returncode != 0:
        raise RuntimeError("resilient-HTTP Playwright journey failed; attempt remains unsealed")
    results = json.loads((source_root / "browser/results.json").read_text(encoding="utf-8"))
    if not isinstance(results, dict) or results.get("completed") is not True:
        raise RuntimeError("resilient-HTTP browser evidence did not complete")
    criteria = results.get("criteria")
    if not isinstance(criteria, list) or any(
        not isinstance(row, dict) or row.get("status") != "pass" for row in criteria
    ):
        raise RuntimeError("resilient-HTTP browser criteria did not all pass")
    print("RESILIENT_HTTP_LIVE_ACCEPTED_UNSEALED")
    print("D012_RESTORED")


if __name__ == "__main__":
    main()
