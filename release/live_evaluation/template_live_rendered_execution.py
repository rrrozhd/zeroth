"""Provider-gated harness for ``templates.live-rendered-execution``.

Provisioning is provider-free.  The paid path requires two independent operator
interlocks plus an opaque external credential-reference availability check.  No
credential value is accepted by this module or written to its evidence bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from release.live_evaluation.evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
)
from release.live_evaluation.provider_free_composed import _publish_deploy_workflow

CRITERION_ID = "templates.live-rendered-execution"
SECRET_REFERENCE = "llm.openai"
MODEL = "openai/gpt-4o-mini"
ARM_ENVIRONMENT_VARIABLE = "ZEROTH_ARM_LIVE_TEMPLATE_EXECUTION"
RUN_CEILING_USD = Decimal("0.25")
CAMPAIGN_CEILING_USD = Decimal("10.00")
TEMPLATE_VERSION = 1
TEMPLATE_TEXT = (
    "Return only a JSON object with answer LIVE-TEMPLATE-ALPHA for probe {{ input.probe }}."
)
INPUT_PAYLOAD = {"probe": "alpha", "expected": "LIVE-TEMPLATE-ALPHA"}
EXPECTED_RENDERED = "Return only a JSON object with answer LIVE-TEMPLATE-ALPHA for probe alpha."
EXPECTED_RENDER_DIGEST = hashlib.sha256(EXPECTED_RENDERED.encode()).hexdigest()
EXPECTED_OUTPUT_DIGEST = hashlib.sha256(b'{"answer":"LIVE-TEMPLATE-ALPHA"}').hexdigest()
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


class Response(Protocol):
    status_code: int
    text: str

    def json(self) -> object: ...


Request = Callable[[str, str, dict[str, Any] | None], Response]
SecretReferenceProbe = Callable[[str, str], bool]
LiveExecutor = Callable[[dict[str, object]], Mapping[str, object]]
CostIdentityReader = Callable[[str, str], Mapping[str, object]]


class RestartableService(Protocol):
    """Opaque lifecycle boundary used to prove a real service restart.

    Authentication remains inside the caller-supplied HTTP adapter.  This
    contract accepts neither a service key nor a provider credential value.
    """

    def instance_id(self) -> str: ...

    def restart(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LiveTemplateConfig:
    fixture_id: str
    tenant_id: str
    template_name: str
    deployment_ref: str

    def __post_init__(self) -> None:
        for label, value in (
            ("fixture_id", self.fixture_id),
            ("tenant_id", self.tenant_id),
            ("template_name", self.template_name),
            ("deployment_ref", self.deployment_ref),
        ):
            if not _SLUG.fullmatch(value):
                raise ValueError(f"{label} must be a lowercase slug")
        if not self.tenant_id.startswith("evaluation-"):
            raise ValueError("tenant_id must be dedicated to evaluation")


@dataclass(frozen=True, slots=True)
class LiveTemplateFixture:
    fixture_id: str
    template_name: str
    template_version: int
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    provider_calls_performed: int = 0


@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    blockers: tuple[str, ...]
    criterion_id: str = CRITERION_ID
    credential_reference: str = SECRET_REFERENCE
    provider_calls_performed: int = 0


@dataclass(frozen=True, slots=True)
class LiveTemplateObservation:
    criterion_id: str
    template_name: str
    template_version: int
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    rendered_prompt_sha256: str
    provider: str
    provider_request_id: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: str
    campaign_cost_after_usd: str
    run_id: str
    audit_id: str
    cost_event_id: str
    terminal_output_sha256: str
    refresh_run_id: str
    restart_run_id: str
    refresh_template_version: int
    restart_template_version: int
    refresh_graph_version_ref: str
    restart_graph_version_ref: str
    refresh_deployment_version: int
    restart_deployment_version: int
    pre_restart_instance_id: str
    post_restart_instance_id: str
    audit_chain_signed: bool
    credential_reference: str
    credential_value_retained: bool
    cost_measurement: str = "measured"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_TERMINAL_RUN_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "terminated_by_policy",
        "terminated_by_loop_guard",
        "dead_letter",
    }
)


def _service_object(response: Response, *, expected: set[int], label: str) -> dict[str, Any]:
    """Read one HTTP object without reflecting a possibly sensitive body on errors."""
    if response.status_code not in expected:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned a non-object response")
    return value


def _decimal_field(row: Mapping[str, object], field: str, *, label: str) -> Decimal:
    try:
        value = Decimal(str(row[field]))
    except (KeyError, InvalidOperation) as exc:
        raise RuntimeError(f"{label} is missing or not decimal") from exc
    if not value.is_finite():
        raise RuntimeError(f"{label} must be finite")
    return value


def _assert_fixture_identity(fixture: LiveTemplateFixture, *, config: LiveTemplateConfig) -> None:
    if (
        fixture.fixture_id != config.fixture_id
        or fixture.template_name != config.template_name
        or fixture.template_version != TEMPLATE_VERSION
        or fixture.deployment_ref != config.deployment_ref
        or fixture.deployment_version != 1
        or not fixture.graph_version_ref.endswith("@1")
        or fixture.provider_calls_performed != 0
    ):
        raise RuntimeError("provisioned template fixture identity drifted")


def _assert_health(
    row: Mapping[str, object], *, config: LiveTemplateConfig, fixture: LiveTemplateFixture
) -> None:
    if (
        row.get("status") != "ok"
        or row.get("deployment_ref") != config.deployment_ref
        or row.get("deployment_version") != fixture.deployment_version
        or row.get("graph_version_ref") != fixture.graph_version_ref
        or row.get("campaign_id") != config.tenant_id
    ):
        raise RuntimeError("service health identity does not match the live template fixture")


def _assert_template_readback(row: Mapping[str, object], *, config: LiveTemplateConfig) -> None:
    if (
        row.get("name") != config.template_name
        or row.get("version") != TEMPLATE_VERSION
        or row.get("template_str") != TEMPLATE_TEXT
    ):
        raise RuntimeError("template readback does not match the immutable fixture")


def _poll_terminal_run(
    request: Request,
    *,
    run_id: str,
    wait: Callable[[float], None],
    attempts: int,
) -> dict[str, Any]:
    for attempt in range(attempts):
        row = _service_object(
            request("GET", f"/v1/runs/{run_id}", None),
            expected={200},
            label="run status readback",
        )
        status = row.get("status")
        if status in _TERMINAL_RUN_STATUSES:
            if status != "succeeded":
                raise RuntimeError(f"live template run terminated with status {status}")
            return row
        if status not in {"queued", "running"}:
            raise RuntimeError("live template run returned an unknown lifecycle status")
        if attempt + 1 < attempts:
            wait(0.25)
    raise RuntimeError("live template run did not reach a terminal state")


def _canonical_output_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("terminal output is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _agent_audit(
    evidence: Mapping[str, object], *, run_id: str, fixture: LiveTemplateFixture
) -> Mapping[str, object]:
    audits = evidence.get("audits")
    if not isinstance(audits, list):
        raise RuntimeError("run evidence omitted audit records")
    candidates = [
        row
        for row in audits
        if isinstance(row, Mapping)
        and row.get("run_id") == run_id
        and row.get("node_id") == "rendered-agent"
        and row.get("status") == "completed"
    ]
    if len(candidates) != 1:
        raise RuntimeError("run evidence must contain exactly one completed rendered-agent audit")
    audit = candidates[0]
    if (
        audit.get("deployment_ref") != fixture.deployment_ref
        or audit.get("graph_version_ref") != fixture.graph_version_ref
    ):
        raise RuntimeError("rendered-agent audit workflow identity drifted")
    return audit


def _rendered_prompt_digest(metadata: Mapping[str, object]) -> str:
    retained_digest = metadata.get("rendered_prompt_sha256")
    if isinstance(retained_digest, str):
        if not _SHA256.fullmatch(retained_digest):
            raise RuntimeError("rendered prompt digest is malformed")
        return retained_digest
    rendered = metadata.get("rendered_prompt")
    if not isinstance(rendered, str):
        raise RuntimeError("rendered prompt digest is absent from the signed audit")
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def collect_live_template_observation(
    *,
    config: LiveTemplateConfig,
    fixture: LiveTemplateFixture,
    request: Request,
    cost_identity: CostIdentityReader,
    lifecycle: RestartableService,
    wait: Callable[[float], None] = time.sleep,
    poll_attempts: int = 240,
) -> LiveTemplateObservation:
    """Execute and collect the criterion through the real service APIs.

    The supplied request adapter owns service authentication.  This collector
    deliberately has no argument capable of receiving a service/provider key.
    It records only typed identities, counts, costs, and SHA-256 digests.
    """
    if poll_attempts < 1:
        raise ValueError("poll_attempts must be positive")
    _assert_fixture_identity(fixture, config=config)

    economics = _service_object(
        request("GET", "/v1/econ/configuration", None),
        expected={200},
        label="runtime economics configuration",
    )
    if (
        economics.get("tenant_id") != config.tenant_id
        or economics.get("deployment_ref") != config.deployment_ref
        or _decimal_field(economics, "per_run_cap_usd", label="per-run ceiling") != RUN_CEILING_USD
        or economics.get("failure_mode") != "fail_closed"
        or economics.get("source") != "service_runtime"
    ):
        raise RuntimeError("strict runtime economics does not enforce the $0.25 run ceiling")
    tenant_cost_path = f"/v1/tenants/{config.tenant_id}/cost"
    pre_cost = _service_object(
        request("GET", tenant_cost_path, None),
        expected={200},
        label="tenant cost readiness",
    )
    if (
        pre_cost.get("tenant_id") != config.tenant_id
        or _decimal_field(pre_cost, "budget_cap_usd", label="campaign ceiling")
        != CAMPAIGN_CEILING_USD
        or _decimal_field(pre_cost, "budget_consumed_usd", label="campaign spend")
        > CAMPAIGN_CEILING_USD
    ):
        raise RuntimeError("strict runtime economics does not enforce the $10 campaign ceiling")

    health = _service_object(
        request("GET", "/health", None), expected={200}, label="service health"
    )
    _assert_health(health, config=config, fixture=fixture)
    template_path = f"/v1/templates/{config.template_name}?version={TEMPLATE_VERSION}"
    initial_template = _service_object(
        request("GET", template_path, None), expected={200}, label="template readback"
    )
    _assert_template_readback(initial_template, config=config)
    pre_restart_instance_id = _required_identifier(
        {"instance_id": lifecycle.instance_id()}, "instance_id", "pre-restart service"
    )

    # RunInvocationRequest is extra-forbid and accepts no client-supplied cap.
    # The exact cap was proven above from the already-built service runtime.
    created = _service_object(
        request(
            "POST",
            "/v1/runs",
            {
                "input_payload": dict(INPUT_PAYLOAD),
                "campaign_id": config.tenant_id,
                "campaign_strict": True,
            },
        ),
        expected={200, 202},
        label="live template run submission",
    )
    run_id = _required_identifier(created, "run_id", "run")
    terminal = _poll_terminal_run(request, run_id=run_id, wait=wait, attempts=poll_attempts)
    if (
        terminal.get("deployment_ref") != fixture.deployment_ref
        or terminal.get("graph_version_ref") != fixture.graph_version_ref
        or terminal.get("tenant_id") != config.tenant_id
        or terminal.get("campaign_id") != config.tenant_id
    ):
        raise RuntimeError("terminal run identity drifted")
    terminal_output_digest = _canonical_output_digest(terminal.get("terminal_output"))
    if terminal_output_digest != EXPECTED_OUTPUT_DIGEST:
        raise RuntimeError("terminal output digest does not match the exact expected JSON")

    evidence = _service_object(
        request("GET", f"/v1/runs/{run_id}/evidence", None),
        expected={200},
        label="run evidence",
    )
    evidence_run = evidence.get("run")
    if not isinstance(evidence_run, Mapping) or evidence_run.get("run_id") != run_id:
        raise RuntimeError("run evidence is not bound to the submitted run")
    audit = _agent_audit(evidence, run_id=run_id, fixture=fixture)
    audit_id = _required_identifier(audit, "audit_id", "audit")
    cost_event_id = _required_identifier(audit, "cost_event_id", "cost event")
    metadata = audit.get("execution_metadata")
    if not isinstance(metadata, Mapping):
        raise RuntimeError("rendered-agent audit execution metadata is missing")
    rendered_digest = _rendered_prompt_digest(metadata)
    if rendered_digest != EXPECTED_RENDER_DIGEST:
        raise RuntimeError("rendered prompt digest does not match the exact template render")
    expected_template_name_digest = hashlib.sha256(config.template_name.encode()).hexdigest()
    if (
        metadata.get("template_name_sha256") != expected_template_name_digest
        or metadata.get("template_version") != TEMPLATE_VERSION
    ):
        raise RuntimeError("signed audit omitted the immutable template reference")
    identity = cost_identity(cost_event_id, run_id)
    if identity.get("cost_event_id") != cost_event_id or identity.get("run_id") != run_id:
        raise RuntimeError("authoritative cost identity drifted from the signed audit")
    provider_request_id = _required_identifier(identity, "provider_request_id", "provider request")
    provider_value = identity.get("provider")
    if provider_value not in {"openai", "litellm"}:
        raise RuntimeError("signed audit provider identity is missing or malformed")
    usage = audit.get("token_usage")
    if not isinstance(usage, Mapping):
        raise RuntimeError("signed audit token usage is missing")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if (
        type(input_tokens) is not int
        or type(output_tokens) is not int
        or type(total_tokens) is not int
        or min(input_tokens, output_tokens, total_tokens) < 0
        or total_tokens != input_tokens + output_tokens
        or usage.get("model_name") != MODEL
    ):
        raise RuntimeError("signed audit provider usage is inconsistent")
    cost_measurement = audit.get("cost_measurement")
    if cost_measurement not in {"measured", "estimated"}:
        raise RuntimeError("live template call lacks a measured or estimated provider cost")
    cost_field = "cost_usd" if cost_measurement == "measured" else "estimated_cost_usd"
    accounted_cost = _decimal_field(audit, cost_field, label=f"{cost_measurement} run cost")
    if accounted_cost < 0 or accounted_cost > RUN_CEILING_USD:
        raise RuntimeError("accounted run cost exceeds the $0.25 ceiling")
    summary = evidence.get("summary")
    if not isinstance(summary, Mapping) or (
        summary.get("priced_call_count") != 1
        or summary.get("cost_event_count") != 1
        or _decimal_field(summary, "total_cost_usd", label="evidence total cost") != accounted_cost
        or summary.get("cost_identity_state") != "correlated"
        or summary.get("reconciliation_state") != "reconciled"
    ):
        raise RuntimeError("run cost evidence is not exactly reconciled")
    verification = _service_object(
        request("POST", f"/v1/runs/{run_id}/verify-chain", None),
        expected={200},
        label="run audit verification",
    )
    if (
        verification.get("verified") is not True
        or verification.get("signature_verified") is not True
        or verification.get("unsigned_record_count") != 0
        or verification.get("record_count") != len(evidence.get("audits", []))
    ):
        raise RuntimeError("run audit chain is not signed and verified")
    post_cost = _service_object(
        request("GET", tenant_cost_path, None),
        expected={200},
        label="post-run tenant cost",
    )
    campaign_after = _decimal_field(
        post_cost, "budget_consumed_usd", label="post-run campaign spend"
    )
    if campaign_after < accounted_cost or campaign_after > CAMPAIGN_CEILING_USD:
        raise RuntimeError("post-run campaign spend does not reconcile within the ceiling")

    refresh_run = _service_object(
        request("GET", f"/v1/runs/{run_id}", None),
        expected={200},
        label="refresh run readback",
    )
    refresh_template = _service_object(
        request("GET", template_path, None), expected={200}, label="refresh template readback"
    )
    _assert_template_readback(refresh_template, config=config)
    if (
        refresh_run.get("run_id") != run_id
        or refresh_run.get("graph_version_ref") != fixture.graph_version_ref
        or refresh_run.get("deployment_ref") != fixture.deployment_ref
        or refresh_run.get("status") != "succeeded"
    ):
        raise RuntimeError("refresh readback did not restore the same terminal run")

    lifecycle.restart()
    post_restart_instance_id = _required_identifier(
        {"instance_id": lifecycle.instance_id()}, "instance_id", "post-restart service"
    )
    if post_restart_instance_id == pre_restart_instance_id:
        raise RuntimeError("restart proof lacks a distinct post-restart service instance")
    restarted_health = _service_object(
        request("GET", "/health", None), expected={200}, label="restarted service health"
    )
    _assert_health(restarted_health, config=config, fixture=fixture)
    restart_run = _service_object(
        request("GET", f"/v1/runs/{run_id}", None),
        expected={200},
        label="restart run readback",
    )
    restart_template = _service_object(
        request("GET", template_path, None), expected={200}, label="restart template readback"
    )
    _assert_template_readback(restart_template, config=config)
    if (
        restart_run.get("run_id") != run_id
        or restart_run.get("graph_version_ref") != fixture.graph_version_ref
        or restart_run.get("deployment_ref") != fixture.deployment_ref
        or restart_run.get("status") != "succeeded"
    ):
        raise RuntimeError("restart readback did not restore the same terminal run")

    return validate_live_template_observation(
        LiveTemplateObservation(
            criterion_id=CRITERION_ID,
            template_name=config.template_name,
            template_version=TEMPLATE_VERSION,
            graph_version_ref=fixture.graph_version_ref,
            deployment_ref=fixture.deployment_ref,
            deployment_version=fixture.deployment_version,
            rendered_prompt_sha256=rendered_digest,
            provider=MODEL.split("/", 1)[0],
            provider_request_id=provider_request_id,
            model=MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=format(accounted_cost, "f"),
            campaign_cost_after_usd=format(campaign_after, "f"),
            run_id=run_id,
            audit_id=audit_id,
            cost_event_id=cost_event_id,
            terminal_output_sha256=terminal_output_digest,
            refresh_run_id=str(refresh_run["run_id"]),
            restart_run_id=str(restart_run["run_id"]),
            refresh_template_version=int(refresh_template["version"]),
            restart_template_version=int(restart_template["version"]),
            refresh_graph_version_ref=str(refresh_run["graph_version_ref"]),
            restart_graph_version_ref=str(restart_run["graph_version_ref"]),
            refresh_deployment_version=fixture.deployment_version,
            restart_deployment_version=fixture.deployment_version,
            pre_restart_instance_id=pre_restart_instance_id,
            post_restart_instance_id=post_restart_instance_id,
            audit_chain_signed=True,
            credential_reference=SECRET_REFERENCE,
            credential_value_retained=False,
            cost_measurement=str(cost_measurement),
        ).to_dict(),
        expected=config,
    )


def _object(response: Response, *, expected: int, label: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}: {response.text}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned a non-object response")
    return value


class LiveTemplateHarness:
    """Provision safely, report readiness, and execute only across both gates."""

    def __init__(
        self,
        *,
        config: LiveTemplateConfig,
        request: Request,
        secret_reference_available: SecretReferenceProbe,
        cost_identity: CostIdentityReader | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.request = request
        self._secret_reference_available = secret_reference_available
        self._cost_identity = cost_identity
        self._environment = dict(os.environ if environment is None else environment)

    def readiness(self, *, armed: bool) -> Readiness:
        blockers: list[str] = []
        if not armed:
            blockers.append("explicit --arm-live-provider flag is absent")
        if self._environment.get(ARM_ENVIRONMENT_VARIABLE) != CRITERION_ID:
            blockers.append(f"{ARM_ENVIRONMENT_VARIABLE} does not equal {CRITERION_ID}")
        try:
            available = self._secret_reference_available(SECRET_REFERENCE, self.config.tenant_id)
        except Exception:
            available = False
        if available is not True:
            blockers.append(
                f"external logical credential reference {SECRET_REFERENCE} is unavailable"
            )
        return Readiness(ready=not blockers, blockers=tuple(blockers))

    def provision(self) -> LiveTemplateFixture:
        """Publish a pinned template-backed agent without resolving a provider secret."""
        template = _object(
            self.request(
                "POST",
                "/v1/templates",
                {
                    "name": self.config.template_name,
                    "version": TEMPLATE_VERSION,
                    "template_str": TEMPLATE_TEXT,
                    "description": f"Disposable fixture for {CRITERION_ID}",
                },
            ),
            expected=201,
            label="create immutable live-render template",
        )
        if template.get("name") != self.config.template_name or template.get("version") != 1:
            raise RuntimeError("template API did not create the exact immutable version 1")

        input_contract = f"contract://{self.config.fixture_id}.probe"
        output_contract = f"contract://{self.config.fixture_id}.answer"
        schemas = (
            (
                input_contract,
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["probe", "expected"],
                    "properties": {
                        "probe": {"const": "alpha"},
                        "expected": {"const": "LIVE-TEMPLATE-ALPHA"},
                    },
                },
            ),
            (
                output_contract,
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["answer"],
                    "properties": {"answer": {"const": "LIVE-TEMPLATE-ALPHA"}},
                },
            ),
        )
        for contract_name, schema in schemas:
            contract = _object(
                self.request(
                    "POST",
                    "/api/studio/v1/contracts",
                    {
                        "name": contract_name,
                        "json_schema": schema,
                        "metadata": {
                            "criterion_id": CRITERION_ID,
                            "provider_calls_performed": 0,
                        },
                    },
                ),
                expected=201,
                label=f"create live-render contract {contract_name}",
            )
            if contract.get("name") != contract_name or contract.get("version") != 1:
                raise RuntimeError("contract API did not create a new immutable version 1")

        workflow = _object(
            self.request(
                "POST",
                "/api/studio/v1/workflows",
                {"name": f"Live template render {self.config.fixture_id}"},
            ),
            expected=201,
            label="create live-render workflow",
        )
        workflow_id = workflow.get("id")
        if not isinstance(workflow_id, str) or not workflow_id:
            raise RuntimeError("workflow creation returned no identity")
        agent_config = {
            "instruction": "Template reference is mandatory",
            "model_provider": MODEL,
            "model_params": {"temperature": 0, "max_tokens": 24},
            "timeout_seconds": 30,
            "thread_participation": "none",
            "template_ref": {"name": self.config.template_name, "version": 1},
        }
        saved = _object(
            self.request(
                "PUT",
                f"/api/studio/v1/workflows/{workflow_id}",
                {
                    "entry_step": "probe-entry",
                    "nodes": [
                        {
                            "id": "probe-entry",
                            "type": "entrypoint",
                            "position": {"x": 0, "y": 0},
                            "data": {
                                "label": "Exact render probe",
                                "config": {},
                                "input_contract_ref": input_contract,
                                "output_contract_ref": input_contract,
                            },
                        },
                        {
                            "id": "rendered-agent",
                            "type": "agent",
                            "position": {"x": 320, "y": 0},
                            "data": {
                                "label": "Pinned rendered agent",
                                "config": agent_config,
                                "input_contract_ref": input_contract,
                                "output_contract_ref": output_contract,
                            },
                        },
                    ],
                    "edges": [
                        {
                            "id": "probe-entry-rendered-agent",
                            "source": "probe-entry",
                            "target": "rendered-agent",
                            "kind": "data",
                        }
                    ],
                    "execution_settings": {
                        "max_total_steps": 2,
                        "max_total_runtime_seconds": 45,
                        "max_visits_per_node": 1,
                        "default_timeout_seconds": 30,
                    },
                },
            ),
            expected=200,
            label="save live-render workflow",
        )
        if saved.get("id") != workflow_id:
            raise RuntimeError("saved workflow identity drifted")
        graph_version_ref, deployment_version = _publish_deploy_workflow(
            request=self.request,
            workflow_id=workflow_id,
            deployment_ref=self.config.deployment_ref,
        )
        return LiveTemplateFixture(
            fixture_id=self.config.fixture_id,
            template_name=self.config.template_name,
            template_version=1,
            workflow_id=workflow_id,
            graph_version_ref=graph_version_ref,
            deployment_ref=self.config.deployment_ref,
            deployment_version=deployment_version,
        )

    def execute(self, *, armed: bool, execute_live: LiveExecutor) -> LiveTemplateObservation:
        readiness = self.readiness(armed=armed)
        if not readiness.ready:
            raise RuntimeError(
                "live template execution is not armed: " + "; ".join(readiness.blockers)
            )
        raw = execute_live(
            {
                "input_payload": dict(INPUT_PAYLOAD),
                "campaign_id": self.config.tenant_id,
                "campaign_strict": True,
                "max_cost_usd": str(RUN_CEILING_USD),
            }
        )
        return validate_live_template_observation(raw, expected=self.config)

    def execute_service(
        self,
        *,
        armed: bool,
        fixture: LiveTemplateFixture,
        lifecycle: RestartableService,
        wait: Callable[[float], None] = time.sleep,
        poll_attempts: int = 240,
    ) -> LiveTemplateObservation:
        """Run the paid checkpoint through service APIs after both interlocks."""
        readiness = self.readiness(armed=armed)
        if not readiness.ready:
            raise RuntimeError(
                "live template execution is not armed: " + "; ".join(readiness.blockers)
            )
        if self._cost_identity is None:
            raise RuntimeError("authoritative cost identity reader is unavailable")
        return collect_live_template_observation(
            config=self.config,
            fixture=fixture,
            request=self.request,
            cost_identity=self._cost_identity,
            lifecycle=lifecycle,
            wait=wait,
            poll_attempts=poll_attempts,
        )


def _required_identifier(row: Mapping[str, object], field: str, label: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise RuntimeError(f"{label} identity is missing or malformed")
    return value


def validate_live_template_observation(
    raw: Mapping[str, object], *, expected: LiveTemplateConfig
) -> LiveTemplateObservation:
    """Validate the sanitized live observation and every acceptance invariant."""
    try:
        observation = LiveTemplateObservation(**dict(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"live template observation schema is invalid: {exc}") from exc
    if (
        observation.criterion_id != CRITERION_ID
        or observation.template_name != expected.template_name
        or observation.template_version != 1
        or observation.deployment_ref != expected.deployment_ref
        or not observation.graph_version_ref.endswith("@1")
        or observation.deployment_version != 1
    ):
        raise RuntimeError("immutable template identity or deployment identity drifted")
    if (
        not _SHA256.fullmatch(observation.rendered_prompt_sha256)
        or observation.rendered_prompt_sha256 != EXPECTED_RENDER_DIGEST
        or observation.terminal_output_sha256 != EXPECTED_OUTPUT_DIGEST
    ):
        raise RuntimeError("render digest or terminal output digest does not prove exact rendering")
    identities = (
        _required_identifier(raw, "provider_request_id", "provider request"),
        _required_identifier(raw, "run_id", "run"),
        _required_identifier(raw, "audit_id", "audit"),
        _required_identifier(raw, "cost_event_id", "cost event"),
    )
    if len(set(identities)) != len(identities):
        raise RuntimeError("provider, run, audit, and cost identities must be distinct")
    if observation.provider != "openai" or observation.model != MODEL:
        raise RuntimeError("provider or model identity drifted")
    usage = (observation.input_tokens, observation.output_tokens, observation.total_tokens)
    if (
        any(type(value) is not int for value in usage)
        or min(usage) < 0
        or (observation.total_tokens != observation.input_tokens + observation.output_tokens)
    ):
        raise RuntimeError("provider usage accounting is inconsistent")
    try:
        measured = Decimal(observation.cost_usd)
        campaign = Decimal(observation.campaign_cost_after_usd)
    except InvalidOperation as exc:
        raise RuntimeError("cost accounting is not decimal") from exc
    if not measured.is_finite() or not campaign.is_finite():
        raise RuntimeError("cost accounting must contain finite decimal values")
    if measured < 0 or measured > RUN_CEILING_USD:
        raise RuntimeError("accounted cost exceeds the $0.25 run ceiling")
    if campaign < measured or campaign > CAMPAIGN_CEILING_USD:
        raise RuntimeError("campaign cost exceeds the $10 campaign ceiling")
    if observation.refresh_run_id != observation.run_id:
        raise RuntimeError("refresh persistence did not restore the same run")
    if (
        observation.refresh_template_version != observation.template_version
        or observation.refresh_graph_version_ref != observation.graph_version_ref
        or observation.refresh_deployment_version != observation.deployment_version
    ):
        raise RuntimeError("refresh persistence did not restore immutable workflow identity")
    if (
        observation.restart_run_id != observation.run_id
        or observation.restart_template_version != observation.template_version
        or observation.restart_graph_version_ref != observation.graph_version_ref
        or observation.restart_deployment_version != observation.deployment_version
    ):
        raise RuntimeError("restart persistence did not restore immutable run/workflow identity")
    if observation.pre_restart_instance_id == observation.post_restart_instance_id:
        raise RuntimeError("restart proof lacks a distinct post-restart service instance")
    if not observation.audit_chain_signed:
        raise RuntimeError("audit chain is not signed")
    if observation.credential_reference != SECRET_REFERENCE:
        raise RuntimeError("external logical credential reference drifted")
    if observation.credential_value_retained:
        raise RuntimeError("credential retention must be proven false")
    if observation.cost_measurement not in {"measured", "estimated"}:
        raise RuntimeError("cost measurement must be measured or estimated")
    return observation


def seal_live_template_evidence(
    *,
    root: Path,
    observation: LiveTemplateObservation | Mapping[str, object],
    config: LiveTemplateConfig,
    screenshots: Sequence[Path],
) -> Path:
    """Validate, copy exactly three UI proofs, and irreversibly checksum the bundle."""
    if len(screenshots) != 3:
        raise ValueError("exactly three screenshots are required")
    for screenshot in screenshots:
        if not screenshot.is_file():
            raise FileNotFoundError(screenshot)
    row = (
        observation.to_dict()
        if isinstance(observation, LiveTemplateObservation)
        else dict(observation)
    )
    accepted = validate_live_template_observation(row, expected=config)
    store = EvidenceStore(root)
    if any(store.root.iterdir()):
        raise RuntimeError("evidence root must be empty before sealing")
    store._write_exclusive(Path("runtime/live-template-observation.json"), accepted.to_dict())
    screenshot_names = (
        "00-rendered-run.png",
        "01-refresh-restored.png",
        "02-restart-restored.png",
    )
    for source, name in zip(screenshots, screenshot_names, strict=True):
        store.ingest_artifact(source, Path("screenshots") / name)
    event_id = store.append_event(
        "campaign.template.provider.run.audit.cost.verified",
        {
            "criterion_id": CRITERION_ID,
            "template_name_sha256": hashlib.sha256(config.template_name.encode()).hexdigest(),
            "template_version": 1,
            "rendered_prompt_sha256": accepted.rendered_prompt_sha256,
            "model": accepted.model,
            "input_tokens": accepted.input_tokens,
            "output_tokens": accepted.output_tokens,
            "total_tokens": accepted.total_tokens,
            "cost_usd": accepted.cost_usd,
            "cost_measurement": accepted.cost_measurement,
            "campaign_cost_after_usd": accepted.campaign_cost_after_usd,
            "refresh_restored": True,
            "restart_restored": True,
            "audit_chain_signed": True,
            "credential_reference": SECRET_REFERENCE,
            "credential_value_retained": False,
        },
        event_id="1",
        correlation=CorrelationIds(
            run_id=accepted.run_id,
            audit_event_id=accepted.audit_id,
            cost_event_id=accepted.cost_event_id,
            provider_request_id=accepted.provider_request_id,
        ),
    )
    evidence = (
        "runtime/live-template-observation.json",
        *(f"screenshots/{name}" for name in screenshot_names),
        f"events.ndjson#{event_id}",
    )
    store.write_manifest(
        {
            "schema_version": 1,
            "criterion_id": CRITERION_ID,
            "tenant_id": config.tenant_id,
            "deployment_ref": config.deployment_ref,
            "graph_version_ref": accepted.graph_version_ref,
            "template_version": 1,
            "provider_calls_performed": 1,
            "run_ceiling_usd": str(RUN_CEILING_USD),
            "campaign_ceiling_usd": str(CAMPAIGN_CEILING_USD),
            "credential_reference": SECRET_REFERENCE,
            "credential_value_retained": False,
        }
    )
    store.finalize_bundle(
        acceptance=(AcceptanceCriterion(CRITERION_ID, "pass", tuple(evidence)),),
        report_markdown=(
            "# Live template-rendered execution\n\n"
            "One explicitly armed provider call used the immutable template-backed "
            "workflow and produced the expected rendered-output digest. Provider request, "
            "model, usage, measured/estimated cost, run, signed audit, and cost identities are "
            "correlated in the runtime record. Browser refresh and a distinct post-restart "
            "service instance restored the same run. Only the logical credential reference "
            "was retained; the recursive evidence scan found no credential value.\n\n"
            "## Adversarial review\n\n"
            "A single successful call proves this exact rendering path, not general model "
            "quality or provider availability. The safer fallback is to leave the criterion "
            "blocked whenever either interlock, the opaque credential reference, budget "
            "accounting, persistence proof, signed audit, or any correlation ID is absent.\n"
        ),
    )
    return store.root


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _credential_attestation_probe(path: Path | None) -> SecretReferenceProbe:
    """Build an availability probe from metadata only; no secret value is accepted."""
    if path is None:
        return lambda _reference, _tenant: False
    attestation = _read_json_object(path, label="credential-reference attestation")
    allowed = {"credential_reference", "tenant_id", "available"}
    if set(attestation) != allowed:
        raise ValueError(
            "credential-reference attestation must contain only credential_reference, "
            "tenant_id, and available"
        )

    def probe(reference: str, tenant_id: str) -> bool:
        return (
            attestation.get("credential_reference") == reference
            and attestation.get("tenant_id") == tenant_id
            and attestation.get("available") is True
        )

    return probe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed live template-rendered execution readiness and sealer"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--fixture-id", required=True)
        target.add_argument("--tenant-id", required=True)
        target.add_argument("--template-name", required=True)
        target.add_argument("--deployment-ref", required=True)

    readiness = subparsers.add_parser(
        "readiness", help="perform no network or provider call; report all closed gates"
    )
    common(readiness)
    readiness.add_argument("--arm-live-provider", action="store_true")
    readiness.add_argument("--credential-reference-attestation", type=Path)

    validate = subparsers.add_parser(
        "validate", help="validate future seal inputs in a disposable evidence root"
    )
    common(validate)
    validate.add_argument("--observation", type=Path, required=True)
    validate.add_argument("--screenshots", type=Path, nargs=3, required=True)

    seal = subparsers.add_parser("seal", help="seal already-captured sanitized evidence")
    common(seal)
    seal.add_argument("--observation", type=Path, required=True)
    seal.add_argument("--screenshots", type=Path, nargs=3, required=True)
    seal.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = LiveTemplateConfig(
        fixture_id=args.fixture_id,
        tenant_id=args.tenant_id,
        template_name=args.template_name,
        deployment_ref=args.deployment_ref,
    )
    if args.command == "readiness":
        harness = LiveTemplateHarness(
            config=config,
            request=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("readiness command cannot perform HTTP requests")
            ),
            secret_reference_available=_credential_attestation_probe(
                args.credential_reference_attestation
            ),
        )
        result = harness.readiness(armed=args.arm_live_provider)
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0 if result.ready else 2

    observation = _read_json_object(args.observation, label="live observation")
    screenshots = tuple(args.screenshots)
    if args.command == "validate":
        with tempfile.TemporaryDirectory(prefix="zeroth-live-template-validate-") as directory:
            seal_live_template_evidence(
                root=Path(directory) / "bundle",
                observation=observation,
                config=config,
                screenshots=screenshots,
            )
        print(
            json.dumps(
                {
                    "criterion_id": CRITERION_ID,
                    "provider_calls_performed": 0,
                    "seal_inputs_valid": True,
                },
                sort_keys=True,
            )
        )
        return 0
    root = seal_live_template_evidence(
        root=args.root,
        observation=observation,
        config=config,
        screenshots=screenshots,
    )
    print(json.dumps({"root": str(root), "sealed": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
