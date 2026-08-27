"""Provider-independent Workflow-1 excessive-revision fixture validation.

The live fixture uses a local code node named ``research`` to request another
revision, plus the product loop-control node and the runtime's total-step guard.
No provider-capable node is reachable.  This module validates the sanitized UI
summary and exact restoration of the previously served D-012 identity; it does
not restart services or seal evidence.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore
from .provider_free_composed import (
    Request,
    _post,
    _publish_deploy_workflow,
)
from .provider_free_composed import (
    _object as response_object,
)


@dataclass(frozen=True, slots=True)
class ServingIdentity:
    deployment_ref: str
    deployment_version: int
    graph_version_ref: str
    status: str = "ok"
    campaign_id: str = "evaluation-studio-v1"


@dataclass(frozen=True, slots=True)
class ProviderFreeExcessiveRevisionFixture:
    schema_version: int
    fixture_id: str
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    payload: dict[str, object]
    provider_calls_performed: int = 0
    provider_economics_status: str = "not_applicable_no_priced_call"
    restart_required: bool = True


class ExactServingRestoreController:
    """Capture and later compare one exact served deployment identity."""

    def __init__(self, *, request: Request) -> None:
        self.request = request

    def capture(self) -> ServingIdentity:
        health = response_object(
            self.request("GET", "/health", None), expected=200, label="served health"
        )
        if health.get("status") != "ok":
            raise RuntimeError("served health is not ready")
        return _serving_identity(health, label="served health")

    def assert_restored(self, before: ServingIdentity) -> dict[str, object]:
        after = self.capture()
        if after != before:
            raise RuntimeError("D-012 serving identity was not restored exactly")
        encoded = {
            "status": before.status,
            "campaign_id": before.campaign_id,
            "deployment_ref": before.deployment_ref,
            "deployment_version": before.deployment_version,
            "graph_version_ref": before.graph_version_ref,
        }
        return {"before": encoded, "after": dict(encoded), "exact": True}


class DockerComposeServingController(ExactServingRestoreController):
    """Bounded deployment switch using the repository's persistent Docker service."""

    def __init__(
        self,
        *,
        request: Request,
        workspace: Path,
        compose_file: Path,
        command_runner: Any = subprocess.run,
        sleep: Any = time.sleep,
    ) -> None:
        super().__init__(request=request)
        self.workspace = workspace.expanduser().resolve(strict=True)
        self.compose_file = compose_file.expanduser().resolve(strict=True)
        if self.compose_file.parent != self.workspace:
            raise ValueError("compose file must be the workspace-owned development topology")
        self.command_runner = command_runner
        self.sleep = sleep

    def serve(
        self,
        *,
        deployment_ref: str,
        deployment_version: int,
        graph_version_ref: str,
    ) -> ServingIdentity:
        expected = ServingIdentity(deployment_ref, deployment_version, graph_version_ref)
        environment = os.environ.copy()
        environment["ZEROTH_DEV_DEPLOYMENT_REF"] = deployment_ref
        completed = self.command_runner(
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
            cwd=self.workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError("bounded Docker backend recreate failed")
        for _ in range(80):
            try:
                observed = self.capture()
            except Exception:
                observed = None
            if observed == expected:
                return expected
            self.sleep(0.25)
        raise RuntimeError("bounded Docker backend recreate did not serve the exact identity")

    def restore(self, before: ServingIdentity) -> dict[str, object]:
        after = self.serve(
            deployment_ref=before.deployment_ref,
            deployment_version=before.deployment_version,
            graph_version_ref=before.graph_version_ref,
        )
        if after != before:
            raise RuntimeError("D-012 serving identity was not restored exactly")
        encoded = {
            "status": before.status,
            "campaign_id": before.campaign_id,
            "deployment_ref": before.deployment_ref,
            "deployment_version": before.deployment_version,
            "graph_version_ref": before.graph_version_ref,
        }
        return {"before": encoded, "after": dict(encoded), "exact": True}


def provision_excessive_revision_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeExcessiveRevisionFixture:
    """Publish a local code/loop graph that reaches the total-step guard."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    contract_name = f"contract://provider-free-w1-excessive-revision-{fixture_id}"
    schema = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "revision_required": {"type": "boolean"},
            "revision_count": {"type": "integer", "minimum": 0},
            "zeroth_loop": {"type": "object"},
        },
    }
    contract = _post(
        request,
        "/api/studio/v1/contracts",
        {
            "name": contract_name,
            "json_schema": schema,
            "metadata": {
                "campaign_slice": "workflow1-excessive-revision-live",
                "provider_calls_performed": 0,
            },
        },
        expected=201,
        label="create excessive-revision contract",
    )
    if contract.get("name") != contract_name or contract.get("version") != 1:
        raise RuntimeError("excessive-revision contract is not an immutable v1 fixture")
    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Workflow 1 deterministic excessive revision {fixture_id}"},
        expected=201,
        label="create excessive-revision workflow",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError("excessive-revision workflow identity is missing")
    research_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "payload['revision_count'] = int(payload.get('revision_count', 0)) + 1",
            "payload['revision_required'] = True",
            "json.dump(payload, sys.stdout, sort_keys=True)",
        )
    )

    def node(node_id: str, node_type: str, *, x: int, label: str, config: Mapping[str, object]):
        return {
            "id": node_id,
            "type": node_type,
            "position": {"x": x, "y": 0},
            "data": {
                "label": label,
                "config": dict(config),
                "input_contract_ref": contract_name,
                "output_contract_ref": contract_name,
            },
        }

    nodes = [
        node("request", "entrypoint", x=0, label="Research request", config={}),
        node(
            "research",
            "code",
            x=320,
            label="Deterministic revision research",
            config={
                "execution_mode": "inline",
                "inline_source": research_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
        ),
        node(
            "revision-loop",
            "loop",
            x=640,
            label="Revision loop guard",
            config={"until": "payload.revision_required != True", "max_retries": 1},
        ),
    ]
    edges = [
        {"id": "request-research", "source": "request", "target": "research"},
        {"id": "research-loop", "source": "research", "target": "revision-loop"},
        {
            "id": "loop-research",
            "source": "revision-loop",
            "target": "research",
            "source_handle": "repeat",
            "target_handle": "input-data",
            "condition": {
                "expression": "payload.zeroth_loop['revision-loop'].route == 'repeat'",
                "branch_rule": "expression",
                "allow_cycle_traversal": True,
                "metadata": {"loop_route": "repeat"},
            },
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
                    "max_total_steps": 4,
                    "max_total_runtime_seconds": 30,
                    "max_visits_per_node": 3,
                    "max_visits_per_edge": 3,
                    "default_timeout_seconds": 10,
                },
            },
        ),
        expected=200,
        label="save excessive-revision workflow",
    )
    if saved.get("id") not in (None, workflow_id):
        raise RuntimeError("saved excessive-revision workflow identity drifted")
    deployment_ref = f"provider-free-w1-excessive-revision-{fixture_id}"
    graph_ref, deployment_version = _publish_deploy_workflow(
        request=request, workflow_id=workflow_id, deployment_ref=deployment_ref
    )
    return ProviderFreeExcessiveRevisionFixture(
        schema_version=1,
        fixture_id=fixture_id,
        workflow_id=workflow_id,
        graph_version_ref=graph_ref,
        deployment_ref=deployment_ref,
        deployment_version=deployment_version,
        payload={"query": "synthetic-excessive-revision"},
    )


def stage_validated_summary(
    *,
    browser_root: Path,
    destination: Path,
    restore: Mapping[str, object],
    expected_deployment_ref: str,
    expected_graph_version_ref: str,
) -> Path:
    """Join one safe Playwright summary to the post-journey restore proof."""
    browser_root = browser_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    try:
        index = json.loads((browser_root / "results.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Playwright evidence index is malformed") from exc
    EvidenceStore(browser_root).validate(index)
    artifacts = index.get("artifacts") if isinstance(index, Mapping) else None
    if not isinstance(artifacts, list):
        raise RuntimeError("Playwright evidence index lacks artifacts")
    matches = [
        row
        for row in artifacts
        if isinstance(row, Mapping)
        and isinstance(row.get("destination"), str)
        and str(row["destination"]).endswith("workflow1-excessive-revision-summary.json")
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
        raise RuntimeError("Playwright evidence lacks one exact excessive-revision summary")
    relative = Path(str(matches[0]["source"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("Playwright summary path is unsafe")
    source = (browser_root / relative).resolve(strict=True)
    if source.is_symlink() or not source.is_file() or browser_root not in source.parents:
        raise RuntimeError("Playwright summary escaped its source root")
    try:
        summary = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Playwright excessive-revision summary is malformed") from exc
    if not isinstance(summary, dict):
        raise RuntimeError("Playwright excessive-revision summary must be an object")
    summary["d012_restore"] = dict(restore)
    EvidenceStore(destination.parent).validate(summary)
    validate_excessive_revision_summary(
        summary,
        expected_deployment_ref=expected_deployment_ref,
        expected_graph_version_ref=expected_graph_version_ref,
    )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def _object(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    return value


def _identities(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError(f"{label} must be a list of identities")
    return value


def _serving_identity(value: object, *, label: str) -> ServingIdentity:
    row = _object(value, label=label)
    deployment_ref = row.get("deployment_ref")
    deployment_version = row.get("deployment_version")
    graph_version_ref = row.get("graph_version_ref")
    if (
        row.get("status") != "ok"
        or row.get("campaign_id") != "evaluation-studio-v1"
        or not isinstance(deployment_ref, str)
        or not deployment_ref
        or not isinstance(deployment_version, int)
        or isinstance(deployment_version, bool)
        or deployment_version < 1
        or not isinstance(graph_version_ref, str)
        or not graph_version_ref
    ):
        raise RuntimeError(f"{label} is not an exact serving identity")
    return ServingIdentity(deployment_ref, deployment_version, graph_version_ref)


def validate_excessive_revision_summary(
    summary: Mapping[str, object],
    *,
    expected_deployment_ref: str,
    expected_graph_version_ref: str,
) -> dict[str, object]:
    """Fail closed unless the UI/runtime proof is the exact provider-free case."""
    if summary.get("schema_version") != 1:
        raise RuntimeError("excessive-revision summary schema is invalid")
    health = _object(summary.get("health"), label="health")
    if (
        health.get("status") != "ok"
        or health.get("campaign_id") != "evaluation-studio-v1"
        or health.get("deployment_ref") != expected_deployment_ref
        or health.get("graph_version_ref") != expected_graph_version_ref
    ):
        raise RuntimeError("excessive-revision serving identity drifted")

    run = _object(summary.get("run"), label="run")
    run_id = run.get("run_id")
    node_visits = _object(run.get("node_visit_counts"), label="run node visits")
    if (
        not isinstance(run_id, str)
        or not run_id
        or run.get("thread_id") != run_id
        or run.get("status") != "terminated_by_loop_guard"
        or run.get("deployment_ref") != expected_deployment_ref
        or run.get("graph_version_ref") != expected_graph_version_ref
        or run.get("failure_reason") != "max_total_steps"
    ):
        raise RuntimeError("loop guard did not terminate with precise max_total_steps reason")
    if run.get("research_visit_count") != 2 or node_visits.get("research") != 2:
        raise RuntimeError("excessive revision must contain exactly two research visits")
    if node_visits != {"request": 1, "research": 2, "revision-loop": 1}:
        raise RuntimeError("excessive revision contains an unexpected node visit")

    timeline = _object(summary.get("timeline"), label="timeline")
    if timeline.get("node_ids") != ["request", "research", "revision-loop", "research"]:
        raise RuntimeError("timeline does not preserve the exact loop-guard sequence")
    if timeline.get("research_visit_count") != 2:
        raise RuntimeError("timeline does not contain exactly two research visits")

    audit = _object(summary.get("audit"), label="audit")
    audit_ids = _identities(audit.get("audit_ids"), label="audit identities")
    research_audits = _identities(
        audit.get("research_audit_ids"), label="research audit identities"
    )
    if (
        audit.get("verified") is not True
        or audit.get("signature_verified") is not True
        or audit.get("unsigned_record_count") != 0
        or audit.get("record_count") != 4
        or len(audit_ids) != 4
        or len(set(audit_ids)) != 4
        or len(research_audits) != 2
        or any(identity not in audit_ids for identity in research_audits)
    ):
        raise RuntimeError("excessive revision lacks an exact signed audit chain")

    economics = _object(summary.get("economics"), label="economics")
    provider_ids = _identities(
        economics.get("provider_request_ids"), label="provider request identities"
    )
    cost_ids = _identities(economics.get("cost_event_ids"), label="cost event identities")
    total_cost = economics.get("total_cost_usd")
    if (
        economics.get("provider_calls_performed") != 0
        or provider_ids
        or cost_ids
        or economics.get("priced_call_count") != 0
        or economics.get("cost_event_count") != 0
        or not isinstance(total_cost, (int, float))
        or isinstance(total_cost, bool)
        or float(total_cost) != 0.0
        or economics.get("cost_identity_state") != "not_applicable_no_priced_call"
        or economics.get("reconciliation_state") != "reconciled_zero_activity"
    ):
        raise RuntimeError("provider-independent excessive revision contains provider activity")

    refresh = _object(summary.get("refresh"), label="refresh")
    if (
        refresh.get("before_run_id") != run_id
        or refresh.get("restored_run_id") != run_id
        or refresh.get("restored_status") != "terminated_by_loop_guard"
        or refresh.get("restored_failure_reason") != "max_total_steps"
        or refresh.get("restored_research_visit_count") != 2
    ):
        raise RuntimeError("UI refresh did not restore the exact loop-guard identity")

    restore = _object(summary.get("d012_restore"), label="D-012 restore")
    before = _serving_identity(restore.get("before"), label="D-012 before identity")
    after = _serving_identity(restore.get("after"), label="D-012 after identity")
    if restore.get("exact") is not True or before != after:
        raise RuntimeError("D-012 serving identity was not restored exactly")

    return {
        "run_id": run_id,
        "research_visit_count": 2,
        "audit_record_count": 4,
        "d012_restored": True,
        "provider_calls_performed": 0,
        "total_cost_usd": 0.0,
    }
