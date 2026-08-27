"""Unsealed provider-free fixture and validation for batching/subgraph UI evidence.

The provisioning path uses the public Studio and deployment APIs exclusively.
It deliberately stops after deployment: serving the new immutable parent still
requires the existing owned backend restart boundary.  The validation helpers
consume UI/runtime observations but never create or seal an evidence bundle.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

ITEMS = tuple({"index": index, "value": f"deterministic-item-{index}"} for index in range(8))


class ResponseLike(Protocol):
    status_code: int
    text: str

    def json(self) -> object: ...


Request = Callable[[str, str, dict[str, Any] | None], ResponseLike]


class HttpFixtureClient:
    """Narrow authenticated transport for the public fixture lifecycle APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        tenant_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not tenant_id:
            raise ValueError("fixture API key and tenant identity are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.client = client or httpx.Client()

    def __call__(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        return self.client.request(
            method,
            f"{self.base_url}{path}",
            headers={"X-API-Key": self.api_key, "X-Tenant-ID": self.tenant_id},
            json=payload,
            timeout=10.0,
            follow_redirects=False,
        )


@dataclass(frozen=True, slots=True)
class ProviderFreeComposedFixture:
    schema_version: int
    fixture_id: str
    child_workflow_id: str
    child_graph_version_ref: str
    child_deployment_ref: str
    child_deployment_version: int
    parent_workflow_id: str
    parent_graph_version_ref: str
    parent_deployment_ref: str
    parent_deployment_version: int
    items: tuple[dict[str, object], ...]
    max_concurrency: int = 4
    provider_calls_performed: int = 0
    provider_economics_status: str = "blocked"
    restart_required: bool = True


_DOCKER_SNAPSHOT_SCRIPT = """\
import pathlib
import sqlite3
import sys

source_path = pathlib.Path(sys.argv[1])
snapshot_path = pathlib.Path(sys.argv[2])
if snapshot_path.exists():
    raise SystemExit("snapshot destination already exists")
snapshot_path.parent.mkdir(parents=True, exist_ok=True)
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(snapshot_path)
try:
    source.backup(destination)
    destination.execute("PRAGMA journal_mode=DELETE")
    check = destination.execute("PRAGMA quick_check").fetchone()
    if check != ("ok",):
        raise RuntimeError("snapshot quick_check failed")
finally:
    destination.close()
    source.close()
print("ok")
"""


@dataclass(frozen=True, slots=True)
class DockerSqliteSnapshotter:
    """Capture SQLite with its online-backup API inside the backend container.

    Docker Desktop's bind-mounted WAL file is not a coherent host-side read
    boundary.  The source connection and ``backup`` therefore run in the same
    filesystem/locking domain as the service writer.  Only the closed,
    quick-checked destination is read by the host validation process.
    """

    compose_file: Path
    compose_project: str
    service: str
    container_database: Path
    container_snapshot: Path
    host_snapshot: Path
    timeout_seconds: int = 30

    def capture(self) -> Path:
        compose_file = self.compose_file.expanduser().resolve(strict=True)
        host_snapshot = self.host_snapshot.expanduser().resolve(strict=False)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.compose_project) is None:
            raise ValueError("compose project name is invalid")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.service) is None:
            raise ValueError("compose service name is invalid")
        if not self.container_database.is_absolute() or not self.container_snapshot.is_absolute():
            raise ValueError("container snapshot paths must be absolute")
        if not 5 <= self.timeout_seconds <= 120:
            raise ValueError("Docker SQLite snapshot timeout must be bounded")
        if host_snapshot.exists() or any(
            host_snapshot.with_name(f"{host_snapshot.name}{suffix}").exists()
            for suffix in ("-wal", "-shm")
        ):
            raise FileExistsError(host_snapshot)
        if not host_snapshot.parent.is_dir():
            raise FileNotFoundError(host_snapshot.parent)

        argv = (
            "docker",
            "compose",
            "-p",
            self.compose_project,
            "-f",
            str(compose_file),
            "exec",
            "-T",
            self.service,
            "python",
            "-c",
            _DOCKER_SNAPSHOT_SCRIPT,
            str(self.container_database),
            str(self.container_snapshot),
        )
        completed = subprocess.run(
            argv,
            cwd=compose_file.parent,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0 or completed.stdout.strip() != "ok":
            raise RuntimeError("Docker-domain SQLite snapshot failed")
        if not host_snapshot.is_file():
            raise RuntimeError("Docker-domain SQLite snapshot is not visible on the host")
        _require_immutable_snapshot(host_snapshot)
        return host_snapshot


def write_unsealed_fixture_manifest(
    destination: Path, fixture: ProviderFreeComposedFixture
) -> Path:
    """Write one exclusive staging manifest without claiming acceptance evidence."""
    destination = destination.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        **asdict(fixture),
        "items": list(fixture.items),
        "sealed": False,
        "evidence_status": "staging",
    }
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
    return destination


def read_fixture_manifest(source: Path) -> ProviderFreeComposedFixture:
    """Load only an explicitly unsealed provider-free composed staging manifest."""
    payload = json.loads(source.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.pop("sealed", None) is not False
        or payload.pop("evidence_status", None) != "staging"
        or payload.get("schema_version") != 1
        or payload.get("provider_economics_status") != "blocked"
        or payload.get("provider_calls_performed") != 0
    ):
        raise RuntimeError("composed fixture manifest is not honest unsealed staging")
    items = payload.get("items")
    if not isinstance(items, list) or items != list(ITEMS):
        raise RuntimeError("composed fixture manifest items are not the exact fixture")
    payload["items"] = tuple(dict(item) for item in items)
    return ProviderFreeComposedFixture(**payload)


def _object(response: ResponseLike, *, expected: int, label: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise RuntimeError(f"{label} returned {response.status_code}: {response.text[:300]}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} did not return a JSON object")
    return value


def _post(
    request: Request,
    path: str,
    payload: dict[str, Any] | None,
    *,
    expected: int,
    label: str,
) -> dict[str, Any]:
    return _object(request("POST", path, payload), expected=expected, label=label)


def _workflow_node(
    node_id: str,
    node_type: str,
    *,
    x: int,
    label: str,
    config: Mapping[str, object],
    input_contract_ref: str,
    output_contract_ref: str,
    parallel_config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config),
        "input_contract_ref": input_contract_ref,
        "output_contract_ref": output_contract_ref,
    }
    if parallel_config is not None:
        data["parallel_config"] = dict(parallel_config)
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": 0},
        "data": data,
    }


def _publish_deploy_workflow(
    *,
    request: Request,
    workflow_id: str,
    deployment_ref: str,
) -> tuple[str, int]:
    preflight = _post(
        request,
        f"/api/studio/v1/workflows/{workflow_id}/preflight",
        None,
        expected=200,
        label=f"preflight {workflow_id}",
    )
    if preflight.get("ready") is not True or preflight.get("issues") not in ([], None):
        raise RuntimeError(f"workflow {workflow_id} did not pass provider-free preflight")
    published = _post(
        request,
        f"/api/studio/v1/workflows/{workflow_id}/publish",
        None,
        expected=200,
        label=f"publish {workflow_id}",
    )
    version = published.get("version")
    if published.get("status") != "published" or not isinstance(version, int):
        raise RuntimeError(f"workflow {workflow_id} publish identity is malformed")
    deployment = _post(
        request,
        "/v1/deployments",
        {
            "deployment_ref": deployment_ref,
            "graph_id": workflow_id,
            "graph_version": version,
        },
        expected=201,
        label=f"deploy {workflow_id}",
    )
    deployment_version = deployment.get("version")
    graph_version_ref = deployment.get("graph_version_ref")
    if (
        deployment.get("deployment_ref") != deployment_ref
        or not isinstance(deployment_version, int)
        or graph_version_ref != f"{workflow_id}@{version}"
    ):
        raise RuntimeError(f"deployment {deployment_ref} identity is malformed")
    return graph_version_ref, deployment_version


def provision_fixture(
    *,
    request: Request,
    fixture_id: str,
    child_source: str | None = None,
    fail_mode: Literal["fail_fast", "best_effort"] = "fail_fast",
) -> ProviderFreeComposedFixture:
    """Create contracts, publish child then parent, and deploy both via real APIs."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    contract_prefix = f"contract://provider-free-composed-{fixture_id}"
    item_contract = f"{contract_prefix}.item"
    batch_contract = f"{contract_prefix}.batch"
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["index", "value"],
        "properties": {
            "index": {"type": "integer", "minimum": 0, "maximum": 7},
            "value": {"type": "string", "pattern": "^deterministic-item-[0-7]$"},
        },
    }
    batch_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": 8,
                "maxItems": 8,
                "items": item_schema,
            }
        },
    }
    for name, schema in ((item_contract, item_schema), (batch_contract, batch_schema)):
        contract = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {
                    "campaign_slice": "provider-free-batching-subgraphs",
                    "provider_calls_performed": 0,
                },
            },
            expected=201,
            label=f"create contract {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    child_deployment_ref = f"provider-free-composed-{fixture_id}-child"
    child = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Provider-free deterministic child {fixture_id}"},
        expected=201,
        label="create child workflow",
    )
    child_id = child.get("id")
    if not isinstance(child_id, str) or not child_id:
        raise RuntimeError("child workflow identity is missing")
    delay_source = child_source or "\n".join(
        (
            "import json",
            "import sys",
            "import time",
            "payload = json.load(sys.stdin)",
            # Long enough that all four branch bodies overlap even when the
            # local evidence host briefly delays subprocess startup.  The
            # interval remains bounded well below both node and branch limits.
            "time.sleep(3.0)",
            "json.dump(payload, sys.stdout, sort_keys=True)",
        )
    )
    child_payload = {
        "nodes": [
            _workflow_node(
                "child-entry",
                "entrypoint",
                x=0,
                label="Return deterministic item",
                config={},
                input_contract_ref=item_contract,
                output_contract_ref=item_contract,
            ),
            _workflow_node(
                "deterministic-delay",
                "code",
                x=320,
                label="Bounded deterministic delay",
                config={
                    "execution_mode": "inline",
                    "inline_source": delay_source,
                    "timeout_seconds": 10,
                    "output_extraction_strategy": "json_stdout",
                },
                input_contract_ref=item_contract,
                output_contract_ref=item_contract,
            ),
        ],
        "edges": [
            {
                "id": "entry-to-delay",
                "source": "child-entry",
                "target": "deterministic-delay",
                "kind": "data",
            }
        ],
        "execution_settings": {
            # The runtime enforces this ceiling at the loop head before it
            # terminalizes an empty queue.  Two dispatches therefore require a
            # third loop-head cycle to complete successfully.
            "max_total_steps": 3,
            "max_total_runtime_seconds": 30,
            "max_visits_per_node": 1,
            "default_timeout_seconds": 15,
        },
    }
    _object(
        request("PUT", f"/api/studio/v1/workflows/{child_id}", child_payload),
        expected=200,
        label="save child workflow",
    )
    child_graph_ref, child_deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=child_id,
        deployment_ref=child_deployment_ref,
    )

    parent_deployment_ref = f"provider-free-composed-{fixture_id}-parent"
    parent = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Provider-free eight-item parent {fixture_id}"},
        expected=201,
        label="create parent workflow",
    )
    parent_id = parent.get("id")
    if not isinstance(parent_id, str) or not parent_id:
        raise RuntimeError("parent workflow identity is missing")
    parent_payload = {
        "nodes": [
            _workflow_node(
                "batch-input",
                "entrypoint",
                x=0,
                label="Eight-item batch",
                config={},
                input_contract_ref=batch_contract,
                output_contract_ref=batch_contract,
                parallel_config={
                    "split_path": "items",
                    "merge_strategy": "collect",
                    "fail_mode": fail_mode,
                    "max_branches": 8,
                    "max_concurrency": 4,
                    "batch_size": 8,
                    "branch_timeout_seconds": 30,
                },
            ),
            _workflow_node(
                "deterministic-child",
                "subgraph",
                x=320,
                label="Deterministic child",
                config={
                    "graph_ref": child_deployment_ref,
                    "version": child_deployment_version,
                    "thread_participation": "isolated",
                    "max_depth": 1,
                },
                input_contract_ref=item_contract,
                output_contract_ref=item_contract,
            ),
        ],
        "edges": [
            {
                "id": "batch-to-child",
                "source": "batch-input",
                "target": "deterministic-child",
                "kind": "data",
            }
        ],
        "execution_settings": {
            "max_total_steps": 20,
            "max_total_runtime_seconds": 60,
            "max_visits_per_node": 8,
            "default_timeout_seconds": 30,
        },
    }
    _object(
        request("PUT", f"/api/studio/v1/workflows/{parent_id}", parent_payload),
        expected=200,
        label="save parent workflow",
    )
    parent_graph_ref, parent_deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=parent_id,
        deployment_ref=parent_deployment_ref,
    )
    return ProviderFreeComposedFixture(
        schema_version=1,
        fixture_id=fixture_id,
        child_workflow_id=child_id,
        child_graph_version_ref=child_graph_ref,
        child_deployment_ref=child_deployment_ref,
        child_deployment_version=child_deployment_version,
        parent_workflow_id=parent_id,
        parent_graph_version_ref=parent_graph_ref,
        parent_deployment_ref=parent_deployment_ref,
        parent_deployment_version=parent_deployment_version,
        items=ITEMS,
    )


def provision_partial_failure_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeComposedFixture:
    """Publish one best-effort fixture whose branch-three child fails deterministically."""
    controlled_failure_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "if payload['index'] == 3:",
            "    raise RuntimeError('controlled child failure at branch 3')",
            "json.dump(payload, sys.stdout, sort_keys=True)",
        )
    )
    return provision_fixture(
        request=request,
        fixture_id=fixture_id,
        child_source=controlled_failure_source,
        fail_mode="best_effort",
    )


def _list(value: object, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeError(f"{label} must be a list")
    return value


def validate_ui_summary(
    summary: Mapping[str, object],
    *,
    expected_deployment_ref: str,
    expected_graph_version_ref: str,
) -> dict[str, object]:
    """Fail closed on the exact three-run provider-free UI observation."""
    health = summary.get("health")
    if not isinstance(health, Mapping) or (
        health.get("deployment_ref") != expected_deployment_ref
        or health.get("graph_version_ref") != expected_graph_version_ref
    ):
        raise RuntimeError("backend restart identity does not match the deployed parent")
    repetitions = _list(summary.get("repetitions"), label="repetitions")
    if len(repetitions) != 3:
        raise RuntimeError("UI summary requires exactly three repetitions")
    parent_ids: list[str] = []
    child_ids: list[str] = []
    child_threads: list[str] = []
    aggregate_priced = 0
    aggregate_cost = 0.0
    for expected_repetition, raw in enumerate(repetitions, start=1):
        if not isinstance(raw, Mapping) or raw.get("repetition") != expected_repetition:
            raise RuntimeError("UI repetitions are not exactly ordered")
        parent_id = raw.get("parent_run_id")
        parent_thread = raw.get("parent_thread_id")
        if not isinstance(parent_id, str) or not parent_id or not isinstance(parent_thread, str):
            raise RuntimeError("UI repetition lacks parent run/thread identity")
        if raw.get("terminal_output") != {"items": list(ITEMS)}:
            raise RuntimeError("parent result does not preserve ordered child results")
        children = _list(raw.get("children"), label="children")
        if len(children) != 8:
            raise RuntimeError("each parent must expose exactly eight child runs")
        for child in children:
            if not isinstance(child, Mapping) or child.get("parent_run_id") != parent_id:
                raise RuntimeError("child lineage does not point to the observed parent")
            child_id = child.get("run_id")
            child_thread = child.get("thread_id")
            if not isinstance(child_id, str) or not child_id or not isinstance(child_thread, str):
                raise RuntimeError("child run/thread identity is missing")
            child_ids.append(child_id)
            child_threads.append(child_thread)
        economics = raw.get("economics")
        if not isinstance(economics, Mapping):
            raise RuntimeError("provider-free economics observation is missing")
        priced = economics.get("priced_call_count")
        cost = economics.get("total_cost_usd")
        if priced != 0:
            raise RuntimeError("provider-free run contains a priced call")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or float(cost) != 0.0:
            raise RuntimeError("provider-free run contains nonzero aggregate cost")
        aggregate_priced += int(priced)
        aggregate_cost += float(cost)
        parent_ids.append(parent_id)
    if len(set(parent_ids)) != 3:
        raise RuntimeError("three UI repetitions must create distinct parent runs")
    if len(set(child_ids)) != 24 or len(set(child_threads)) != 24:
        raise RuntimeError("child run and thread identities must be distinct")
    if summary.get("restored_run_id") != parent_ids[-1]:
        raise RuntimeError("Studio refresh did not restore the last parent run")
    if summary.get("provider_economics_status") != "blocked":
        raise RuntimeError("provider economics must remain explicitly blocked")
    return {
        "parent_run_ids": parent_ids,
        "child_run_count": len(child_ids),
        "aggregate_priced_call_count": aggregate_priced,
        "aggregate_cost_usd": aggregate_cost,
        "provider_economics_status": "blocked",
    }


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return decoded


def _json_list(value: object, *, label: str) -> list[Any]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, list):
        raise RuntimeError(f"{label} must be a JSON array")
    return decoded


def _require_immutable_snapshot(database: Path) -> None:
    """Reject live WAL state and prove the closed snapshot is internally sound."""
    if any(database.with_name(f"{database.name}{suffix}").exists() for suffix in ("-wal", "-shm")):
        raise RuntimeError("persistent validation requires a consistent snapshot, not live WAL")
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("persistent snapshot failed SQLite quick_check")
    finally:
        connection.close()


def validate_persistent_snapshot(
    database: Path,
    *,
    tenant_id: str,
    fixture: ProviderFreeComposedFixture,
    parent_run_ids: tuple[str, str, str],
) -> dict[str, object]:
    """Cross-check UI identities against a closed Docker-domain DB snapshot."""
    database = database.expanduser().resolve(strict=True)
    _require_immutable_snapshot(database)
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    ordered_indices: list[list[int]] = []
    measured_peaks: list[int] = []
    child_ids: list[str] = []
    child_threads: list[str] = []
    aggregate_cost = 0.0
    aggregate_estimated = 0.0
    try:
        for parent_id in parent_run_ids:
            parent = connection.execute(
                """
                SELECT run_id, parent_run_id, thread_id, deployment_ref, status,
                       final_output, metadata, execution_history
                FROM runs WHERE tenant_id = ? AND run_id = ?
                """,
                (tenant_id, parent_id),
            ).fetchone()
            if parent is None or parent["parent_run_id"] is not None:
                raise RuntimeError("persistent parent run identity is missing or relabelled")
            if parent["deployment_ref"] != fixture.parent_deployment_ref:
                raise RuntimeError("persistent parent deployment identity drifted")
            if str(parent["status"]).lower() != "completed":
                raise RuntimeError("persistent parent run is not completed")
            if _json_object(parent["final_output"], label="parent final output") != {
                "items": list(ITEMS)
            }:
                raise RuntimeError("persistent parent result is not input ordered")
            children = connection.execute(
                """
                SELECT run_id, parent_run_id, thread_id, deployment_ref, status,
                       final_output, metadata, execution_history
                FROM runs
                WHERE tenant_id = ? AND parent_run_id = ?
                ORDER BY run_id ASC
                """,
                (tenant_id, parent_id),
            ).fetchall()
            if len(children) != 8:
                raise RuntimeError("persistent parent must own exactly eight scoped children")
            branches: list[tuple[int, dict[str, Any]]] = []
            intervals: list[tuple[datetime, datetime]] = []
            for child in children:
                if (
                    child["parent_run_id"] != parent_id
                    or child["deployment_ref"] != fixture.child_deployment_ref
                    or str(child["status"]).lower() != "completed"
                ):
                    raise RuntimeError("persistent child lineage or deployment identity drifted")
                history = _json_list(child["execution_history"], label="child history")
                first = history[0] if history else None
                node_id = first.get("node_id") if isinstance(first, Mapping) else None
                match = (
                    re.match(r"^branch:(\d+):subgraph:", node_id)
                    if isinstance(node_id, str)
                    else None
                )
                if match is None:
                    raise RuntimeError("persistent child lacks an authored branch index")
                branch_index = int(match.group(1))
                delay_entries = [
                    entry
                    for entry in history
                    if isinstance(entry, Mapping)
                    and isinstance(entry.get("node_id"), str)
                    and entry["node_id"].endswith(":deterministic-delay")
                ]
                if len(delay_entries) != 1:
                    raise RuntimeError("persistent child lacks one deterministic delay interval")
                try:
                    started = datetime.fromisoformat(str(delay_entries[0]["started_at"]))
                    completed = datetime.fromisoformat(str(delay_entries[0]["completed_at"]))
                except (KeyError, ValueError) as exc:
                    raise RuntimeError("persistent child delay interval is malformed") from exc
                if completed <= started:
                    raise RuntimeError("persistent child delay interval is not positive")
                intervals.append((started, completed))
                output = _json_object(child["final_output"], label="child final output")
                metadata = _json_object(child["metadata"], label="child metadata")
                cost = metadata.get("total_cost_usd")
                estimated = metadata.get("total_estimated_cost_usd")
                if (
                    not isinstance(cost, (int, float))
                    or isinstance(cost, bool)
                    or not isinstance(estimated, (int, float))
                    or isinstance(estimated, bool)
                    or float(cost) != 0.0
                    or float(estimated) != 0.0
                ):
                    raise RuntimeError("persistent child cost rollup is not aggregate zero")
                aggregate_cost += float(cost)
                aggregate_estimated += float(estimated)
                branches.append((branch_index, output))
                child_ids.append(str(child["run_id"]))
                child_threads.append(str(child["thread_id"]))
            branches.sort(key=lambda item: item[0])
            if [index for index, _ in branches] != list(range(8)):
                raise RuntimeError("persistent child branches are not exactly ordered 0 through 7")
            if [output for _, output in branches] != list(ITEMS):
                raise RuntimeError("persistent child outputs do not preserve input order")
            ordered_indices.append([index for index, _ in branches])
            events = sorted(
                [
                    event
                    for started, completed in intervals
                    for event in ((started, 1), (completed, -1))
                ],
                key=lambda event: (event[0], event[1]),
            )
            active = 0
            peak = 0
            for _, delta in events:
                active += delta
                peak = max(peak, active)
            if peak != fixture.max_concurrency:
                raise RuntimeError(
                    f"persistent child intervals measured peak concurrency {peak}, "
                    f"expected {fixture.max_concurrency}"
                )
            measured_peaks.append(peak)
    finally:
        connection.close()
    if len(set(child_ids)) != 24 or len(set(child_threads)) != 24:
        raise RuntimeError("persistent child run/thread identities are not distinct")
    return {
        "parent_run_count": len(parent_run_ids),
        "child_run_count": len(child_ids),
        "distinct_child_thread_count": len(set(child_threads)),
        "ordered_branch_indices": ordered_indices,
        "measured_peak_concurrency": measured_peaks,
        "aggregate_cost_usd": aggregate_cost,
        "aggregate_estimated_cost_usd": aggregate_estimated,
        "provider_economics_status": "blocked",
    }


class BoundedComposedUiRunner:
    """Run the fixed live Studio spec and validate its unsealed JSON report."""

    _TITLE = "provider-free composed fixture runs three times with durable lineage"

    def __init__(
        self,
        *,
        frontend_root: Path,
        environment: Mapping[str, str],
        snapshotter: DockerSqliteSnapshotter | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self.frontend_root = frontend_root.resolve(strict=True)
        self.spec = self.frontend_root / "e2e/provider-free-composed-live.spec.ts"
        if self.frontend_root.name != "frontend" or not self.spec.is_file():
            raise ValueError("composed UI runner requires the repository frontend root")
        if not 30 <= timeout_seconds <= 300:
            raise ValueError("composed UI runner timeout must be bounded")
        self.environment = dict(environment)
        self.snapshotter = snapshotter
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _attachment(report: object) -> dict[str, object]:
        matches: list[dict[str, object]] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if value.get("name") == "provider-free-composed-summary" and isinstance(
                    value.get("body"), str
                ):
                    try:
                        payload = json.loads(base64.b64decode(value["body"], validate=True))
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError("composed Playwright attachment is malformed") from exc
                    if isinstance(payload, dict):
                        matches.append(payload)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(report)
        if len(matches) != 1:
            raise RuntimeError("Playwright report requires one composed summary attachment")
        return matches[0]

    def run(self, fixture: ProviderFreeComposedFixture) -> dict[str, object]:
        argv = (
            "npm",
            "exec",
            "--",
            "playwright",
            "test",
            "e2e/provider-free-composed-live.spec.ts",
            "--project=desktop-1440",
            "--grep",
            self._TITLE,
            "--reporter=json",
        )
        allowed = {
            "PATH",
            "HOME",
            "TMPDIR",
            "ZEROTH_EVALUATION_API_BASE",
            "ZEROTH_EVALUATION_API_KEY",
            "ZEROTH_EVALUATION_TENANT",
        }
        child_env = {
            name: value
            for name, value in {**os.environ, **self.environment}.items()
            if name in allowed
        }
        child_env.update(
            {
                # The persistent campaign frontend is already running. Starting
                # another Next process would either fail on the occupied port or
                # test a different transient instance than the one under review.
                "PLAYWRIGHT_NO_SERVER": "1",
                "ZEROTH_EVALUATION_LIVE": "1",
                "ZEROTH_EVALUATION_COMPOSED_PARENT_WORKFLOW_ID": fixture.parent_workflow_id,
                "ZEROTH_EVALUATION_COMPOSED_PARENT_DEPLOYMENT_REF": fixture.parent_deployment_ref,
                "ZEROTH_EVALUATION_COMPOSED_PARENT_GRAPH_VERSION": fixture.parent_graph_version_ref,
                "ZEROTH_EVALUATION_COMPOSED_CHILD_DEPLOYMENT_REF": fixture.child_deployment_ref,
                "ZEROTH_EVALUATION_COMPOSED_PAYLOAD": json.dumps(
                    {"items": list(fixture.items)}, separators=(",", ":")
                ),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=self.frontend_root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("bounded provider-free composed Playwright run failed")
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("composed Playwright JSON report is malformed") from exc
        statuses: list[str] = []

        def collect(value: object, *, matched: bool = False) -> None:
            if isinstance(value, dict):
                matched = matched or value.get("title") == self._TITLE
                if matched and isinstance(value.get("results"), list):
                    statuses.extend(
                        str(result.get("status"))
                        for result in value["results"]
                        if isinstance(result, dict)
                    )
                for child in value.values():
                    collect(child, matched=matched)
            elif isinstance(value, list):
                for child in value:
                    collect(child, matched=matched)

        collect(report)
        if statuses != ["passed"]:
            raise RuntimeError("Playwright report does not contain one passing composed test")
        summary = self._attachment(report)
        validated = validate_ui_summary(
            summary,
            expected_deployment_ref=fixture.parent_deployment_ref,
            expected_graph_version_ref=fixture.parent_graph_version_ref,
        )
        if self.snapshotter is not None:
            tenant_id = child_env.get("ZEROTH_EVALUATION_TENANT", "evaluation-studio-v1")
            persistent = validate_persistent_snapshot(
                self.snapshotter.capture(),
                tenant_id=tenant_id,
                fixture=fixture,
                parent_run_ids=tuple(validated["parent_run_ids"]),  # type: ignore[arg-type]
            )
            validated = {**validated, "persistent": persistent}
        return validated
