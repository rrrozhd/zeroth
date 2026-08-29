"""Hiding a field from the protected-surface gate is a recorded act, not a silent one.

Several classes assign ``__signature__`` to drop parameters from the signature
``inspect`` reports. The idiom is deliberate: ``tests/contracts/fixtures/
backend_surface_canonical.json`` pins constructor signatures, so a new field
would otherwise register as a public-surface change and require regenerating a
12,000-line fixture.

What was missing is the record. The exclusion sets lived in ten separate modules
with nothing naming the total, so the gate reported the surface as pinned while
the constructor had grown -- ``PolicyDefinition`` reports eight parameters and
carries fifteen fields. This module is that record: every hidden field is listed
here, the list is checked against reality in both directions, and no class may
start hiding fields without appearing in it.

The list may only shrink. Exposing a hidden field is a surface change with a
fixture-regeneration procedure nobody has written down yet, which is recorded as
a deferred observation rather than attempted here (ZER-41 / A03-14).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/zeroth"

#: Every constructor field hidden from the reported signature, by class.
#: Measured, not asserted: each entry is exactly what
#: ``declared_fields - reported_parameters`` returns today.
HIDDEN_CONSTRUCTOR_FIELDS: dict[str, tuple[str, ...]] = {
    "zeroth.contracts.graph.models:IfNodeData": ("routes",),
    "zeroth.econ.analytics.waste:EconReport": (
        "cost_measurement_complete",
        "estimated_cost_usd",
    ),
    "zeroth.econ.analytics.waste:WasteRollup": (
        "cost_measurement_complete",
        "estimated_cost_usd",
    ),
    "zeroth.econ.analytics.client:RegulusClient": ("_asgi_app",),
    "zeroth.econ.instrumentation.client:InstrumentationClient": ("_asgi_app",),
    "zeroth.econ.instrumentation.schemas:ExecutionEvent": (
        "campaign_id",
        "cleanup_status",
        "cost_measurement",
        "deployment_ref",
        "evidence_kind",
        "operation_id",
        "provider_request_id",
        "usage_measurement",
    ),
    "zeroth.econ.analytics.adapter:InstrumentedProviderAdapter": (
        "branch_id",
        "campaign_id",
        "cost_instrumentation",
        "per_run_cap_usd",
    ),
    "zeroth.econ.instrumentation.transport:TelemetryTransport": ("_asgi_app",),
    "zeroth.econ.plane.enforcement.schemas:BudgetStatusOut": (
        "active_exposure_usd",
        "actual_spend_usd",
        "ambiguous_exposure_usd",
        "budget_consumed_usd",
        "cost_measurement",
        "estimated_spend_usd",
        "measurement_complete",
        "paid_spend_usd",
        "synthetic_control_usd",
        "unmeasured_spend_usd",
    ),
    "zeroth.econ.plane.enforcement.schemas:CostReservationOut": (
        "deployment_ref",
        "evidence_kind",
    ),
    "zeroth.econ.plane.instrumentation.schemas:ExecutionEventCreate": (
        "campaign_id",
        "cleanup_status",
        "cost_measurement",
        "deployment_ref",
        "evidence_kind",
        "operation_id",
        "provider_request_id",
        "usage_measurement",
    ),
    "zeroth.governance.audit.models:NodeAuditRecord": (
        "campaign_id",
        "cost_measurement",
        "estimated_cost_usd",
    ),
    # ZER-37: sidecar workspace staging. Additive and optional on all three
    # models; a request naming none of them behaves exactly as before, and the
    # response payload field is carried on the immediate execute response only.
    "zeroth.integrations.sandbox.models:SidecarExecuteRequest": (
        "capture_output_file",
        "read_only_paths",
        "workspace_id",
    ),
    "zeroth.integrations.sandbox.models:SidecarExecuteResponse": (
        "output_file_b64",
        "output_file_truncated",
    ),
    "zeroth.integrations.sandbox.models:SidecarStatusResponse": (
        "output_file_b64",
        "output_file_truncated",
    ),
    "zeroth.contracts.langgraph_gateway.models:RunCapabilityEvidence": (
        "adapter_version",
        "inventory_fingerprint",
        "run_id",
    ),
    # ZER-49: the API-boundary report of whether a decision reached a policy
    # action. Additive, optional, and decision-only; the response shape it
    # documents is pinned in tests/econ_plane/test_decision_linkage_visibility.py.
    "zeroth.econ.plane.enforcement.schemas:EnforcementActionOut": ("policy_action_effect",),
    "zeroth.governance.audit.models:ToolCallRecord": (
        "operation_first_execution",
        "operation_key",
        "operation_reconciliation_exhausted",
        "operation_reconciliation_required",
        "operation_replay_suppressed",
        "operation_residual_duplicate_risk",
        "operation_state",
        "operation_support",
        "operation_target_ref",
        "tool_call_id",
    ),
    "zeroth.governance.policy.models:PolicyDefinition": (
        "allowed_assistants",
        "allowed_deployments",
        "allowed_input_classifications",
        "allowed_principals",
        "allowed_tenants",
        "max_input_bytes",
        "required_roles",
    ),
    "zeroth.governance.retention.models:ErasureResult": ("operations_deleted",),
    "zeroth.platform.config.settings:ZerothSettings": (
        "approval_notifications",
        "deployment_mode",
        "certification",
        # ZER-37: the GitHub App integration settings block. Additive and
        # default-off; hidden for the same reason as the gateway block.
        "github",
        "langgraph_gateway",
    ),
    "zeroth.runtime.agents.provider:ProviderResponse": (
        "cost_measurement",
        "estimated_cost_usd",
        "usage_measurement",
    ),
    "zeroth.runtime.context.models:CompactionResult": (
        "cost_measurement",
        "cost_usd",
        "estimated_cost_usd",
        "token_usage",
        "usage_measurement",
    ),
    "zeroth.runtime.parallel.models:BranchResult": (
        "cost_measurement",
        "estimated_cost_usd",
    ),
    "zeroth.runtime.parallel.models:FanInResult": (
        "cost_measurement",
        "total_estimated_cost_usd",
    ),
    "zeroth.runtime.runs.models:RunHistoryEntry": (
        "cost_measurement",
        "estimated_cost_usd",
    ),
    "zeroth.runtime.orchestration.orchestrator:RuntimeOrchestrator": (
        "_mcp_pools",
        "_token_snapshot_store",
        "cost_instrumentation",
        "mcp_server_resolver",
        "operation_store",
    ),
    "zeroth.service.api.connector_api:ConnectorTestResponse": (
        "audit_event_id",
        "campaign_id",
        "cleanup_status",
        "cost_event_id",
        "cost_measurement",
        "estimated_cost_usd",
        "operation_id",
        "provider_request_id",
    ),
    "zeroth.service.api.authentication:ServiceAuthConfig": (
        "custom_roles",
        "revoked_credential_ids",
    ),
    "zeroth.service.api.health:HealthResponse": (
        "audit_delivery",
        "campaign_id",
        "langgraph_gateway",
        "certification",
        "production_ready",
    ),
    "zeroth.service.api.run_api:RunInvocationRequest": (
        "campaign_id",
        "campaign_strict",
    ),
    "zeroth.service.api.cost_api:DeploymentCostResponse": (
        "active_exposure_usd",
        "ambiguous_exposure_usd",
        "estimated_spend_usd",
        "paid_spend_usd",
        "unmeasured_spend_usd",
    ),
    "zeroth.service.api.cost_api:TenantCostResponse": (
        "active_exposure_usd",
        "actual_spend_usd",
        "ambiguous_exposure_usd",
        "budget_consumed_usd",
        "estimated_spend_usd",
        "paid_spend_usd",
        "synthetic_control_usd",
        "unmeasured_spend_usd",
    ),
    "zeroth.service.api.run_api:RunInvocationResponse": ("campaign_id", "traversal"),
    "zeroth.service.api.run_api:RunStatusResponse": ("campaign_id", "traversal"),
    "zeroth.service.api.studio_schemas:UpdateWorkflowRequest": ("execution_settings",),
    "zeroth.service.api.studio_schemas:WorkflowDetailResponse": ("execution_settings",),
    "zeroth.service.api.studio_schemas:StudioEdgeResponse": (
        "condition",
        "enabled",
        "mapping",
    ),
    "zeroth.service.bootstrap.container:ServiceBootstrap": (
        "audit_delivery_queue",
        "certification_service",
        "decision_repository",
        "evaluation_campaign",
        "evaluation_fault_state",
        "evaluation_campaign_id",
        "evaluation_receipt_restart_barriers",
        "enforcement_heartbeat_repository",
        "enforcement_stale_after_seconds",
        "guardrail_policy_repository",
        # ZER-37: optional GitHub App integration components, absent unless
        # settings.github.enabled -- additive, like the gateway fields.
        "github_client",
        "github_integration_service",
        "github_maintenance_worker",
        "github_repository",
        "github_token_broker",
        "github_webhook_secret_resolver",
        "inventory_registration_repository",
        "langgraph_enforcement_service",
        "langgraph_gateway_capability_reporter",
        "langgraph_gateway_compatibility",
        "langgraph_gateway_proxy",
        "langgraph_gateway_transport",
        "langgraph_gateway_websocket_handler",
        "mcp_server_config_repository",
        "policy_guard",
        "probe_instrumentation",
        # ZER-37 orchestration glue: repository-unit persistence, service, and
        # run worker -- optional alongside the GitHub integration components.
        "repo_checkout_repository",
        "repo_run_repository",
        "repo_run_worker",
        "repository_unit_service",
        "role_registry",
        "run_attestation_repository",
        "template_dependency_checker",
        "serving_artifact_identity",
        "tool_decision_service",
        "verifier",
    ),
    "zeroth.service.deployments.service:DeploymentService": ("deployment_mode",),
    "zeroth.service.langgraph_gateway.context:ReservedContextClaims": ("run_id",),
    "zeroth.service.webhooks.delivery:WebhookDeliveryWorker": ("audit_recorder",),
    "zeroth.service.webhooks.service:WebhookService": ("audit_recorder",),
}

#: Constructor fields whose runtime validation was narrowed while the protected
#: legacy signature keeps reporting its original annotation.
SIGNATURE_ANNOTATION_OVERRIDES: dict[str, tuple[str, ...]] = {
    "zeroth.contracts.graph.models:Graph": ("nodes",),
    "zeroth.platform.artifacts.models:ArtifactStoreSettings": ("backend",),
}


def _resolve(reference: str) -> Any:
    module, _, name = reference.partition(":")
    return getattr(importlib.import_module(module), name)


def declared_fields(target: Any) -> set[str]:
    """Every field the class really accepts, whatever kind of class it is.

    Pydantic models answer from ``model_fields``; dataclasses and plain classes
    answer from the *real* ``__init__``, which an assigned ``__signature__`` does
    not shadow.
    """
    fields = getattr(target, "model_fields", None)
    if fields is not None:
        return set(fields)
    if dataclasses.is_dataclass(target):
        return {field.name for field in dataclasses.fields(target)}
    return {name for name in inspect.signature(target.__init__).parameters if name != "self"}


def hidden_fields(target: Any) -> set[str]:
    """Fields the class accepts but does not report in its signature."""
    return declared_fields(target) - set(inspect.signature(target).parameters)


def test_service_bootstrap_records_internal_template_dependency_checker() -> None:
    """The internally wired template guard must stay out of the public signature."""
    reference = "zeroth.service.bootstrap.container:ServiceBootstrap"

    assert "template_dependency_checker" in declared_fields(_resolve(reference))
    assert "template_dependency_checker" in hidden_fields(_resolve(reference))
    assert "template_dependency_checker" in HIDDEN_CONSTRUCTOR_FIELDS[reference]


@pytest.mark.parametrize("reference", sorted(HIDDEN_CONSTRUCTOR_FIELDS))
def test_the_recorded_exclusions_match_what_the_class_actually_hides(reference: str) -> None:
    """Both directions, so the record cannot drift from the code either way.

    A new hidden field fails here until it is written down. A field that stops
    being hidden fails until it is removed from the list, which is what makes the
    list shrink-only rather than merely append-only.
    """
    assert hidden_fields(_resolve(reference)) == set(HIDDEN_CONSTRUCTOR_FIELDS[reference])


@pytest.mark.parametrize("reference", sorted(SIGNATURE_ANNOTATION_OVERRIDES))
def test_the_recorded_annotation_overrides_match_real_fields(reference: str) -> None:
    target = _resolve(reference)
    assert set(SIGNATURE_ANNOTATION_OVERRIDES[reference]) <= declared_fields(target)
    for field in SIGNATURE_ANNOTATION_OVERRIDES[reference]:
        assert (
            inspect.signature(target).parameters[field].annotation
            != target.model_fields[field].annotation
        )


def _module_reference(path: Path) -> str:
    """``src/zeroth/a/b.py`` -> ``zeroth.a.b``."""
    relative = path.relative_to(SOURCE.parent).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join(parts)


#: Assignment sites verified to target something other than a class.
#:
#: An earlier attempt scoped discovery to module level on the reasoning that a
#: ``__signature__`` assigned inside a function must belong to a call-time object.
#: **That reasoning is wrong**, and the reviewer disproved it: a function-local
#: ``Klass.__signature__ = ...`` hides real constructor fields just as effectively,
#: and a module-level-only scan reports nothing. Lexical scope does not establish
#: what the target *is*.
#:
#: So the scan is recursive and the one genuinely non-class site is named here,
#: with what it was verified to be. Adding an entry is a claim about a specific
#: target, reviewable as such, rather than a rule that quietly exempts a whole
#: category.
NON_CLASS_SIGNATURE_SITES = {
    "zeroth.integrations.memory.factory:<module>:register_memory_connectors": (
        "a public function whose additive secret-provider scoping keywords are hidden "
        "from the immutable legacy callable signature"
    ),
    "zeroth.integrations.langgraph._tool_wrappers:_govern_callable:governed": (
        "a per-call wrapper *function* returned by _sync_callable_wrapper / "
        "_async_callable_wrapper and published by _govern_callable -- the ordinary "
        "functools.wraps idiom, with no class constructor behind it"
    ),
    "zeroth.service.app:<module>:create_app": (
        "the application factory function; its additive evaluation route registrar "
        "keyword is hidden from the immutable legacy callable signature"
    ),
}


def _assignment_sites(tree: ast.Module, module: str) -> list[tuple[str, str]]:
    """Every ``<target>.__signature__ = ...`` as ``(site, target)``.

    ``site`` is ``module:enclosing.qualname:target`` -- the *lexical location* of
    the assignment, not the name it happens to write to.

    A previous version keyed a dict by ``module:last_component``, which is a
    representation of the site rather than the site. Two consequences, both
    measured: assignments collapsed (a second one with the same target name
    vanished from the map entirely), and the allowlist ended up exempting a
    *spelling* -- injecting a function-local class named ``governed`` into
    ``_tool_wrappers`` inherited the exemption and hid a real field, with the map
    size unchanged.
    """
    sites: list[tuple[str, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            # `Klass.__signature__: Any = ...` is an AnnAssign, not an Assign, and
            # hides fields exactly as well. Checking only Assign missed it.
            targets = (
                child.targets
                if isinstance(child, ast.Assign)
                else [child.target]
                if isinstance(child, ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "__signature__":
                    owner = ast.unparse(target.value).rsplit(".", 1)[-1]
                    where = ".".join(scope) or "<module>"
                    sites.append((f"{module}:{where}:{owner}", owner))
            inner = (
                (*scope, child.name)
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                else scope
            )
            visit(child, inner)

    visit(tree, ())
    return sites


def signature_assignments() -> list[str]:
    """Every ``__signature__`` assignment site in the tree, multiplicity preserved.

    Parsed, not matched, recursively, and returned as a *list*: collapsing into a
    dict keyed by target name is exactly what let a second assignment disappear.
    """
    found: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the tree parses
            continue
        found.extend(site for site, _ in _assignment_sites(tree, _module_reference(path)))
    return sorted(found)


def class_signature_assignments() -> list[str]:
    """Assignment sites that are not on the verified non-class allowlist."""
    return [site for site in signature_assignments() if site not in NON_CLASS_SIGNATURE_SITES]


def _recorded_reference(site: str) -> str:
    """``module:enclosing:target`` -> the ``module:target`` the record is keyed by."""
    module, _, rest = site.partition(":")
    return f"{module}:{rest.rsplit(':', 1)[-1]}"


def test_no_class_hides_a_field_without_appearing_in_the_record() -> None:
    """Every ``__signature__`` assignment in the tree belongs to a recorded class.

    Without this, the record covers only the classes it already names and a new
    module could reintroduce the silence the record exists to end.
    """
    unrecorded = sorted(
        site
        for site in class_signature_assignments()
        if _recorded_reference(site)
        not in HIDDEN_CONSTRUCTOR_FIELDS | SIGNATURE_ANNOTATION_OVERRIDES
    )

    assert unrecorded == [], (
        "these classes hide constructor fields from the protected-surface gate "
        f"without being recorded in HIDDEN_CONSTRUCTOR_FIELDS: {unrecorded}"
    )


def test_the_record_names_no_class_that_stopped_hiding() -> None:
    """The other direction: a recorded entry whose assignment is gone must go too."""
    assigning = {_recorded_reference(site) for site in class_signature_assignments()}
    stale = sorted(
        (set(HIDDEN_CONSTRUCTOR_FIELDS) | set(SIGNATURE_ANNOTATION_OVERRIDES)) - assigning
    )

    assert stale == [], f"recorded but no longer assigning __signature__: {stale}"


@pytest.mark.parametrize(
    "source",
    [
        "PolicyDefinition.__signature__ = value",
        "PolicyDefinition.__signature__  = value",
        "models.PolicyDefinition.__signature__ = value",
        "PolicyDefinition.__signature__ = inspect.signature(PolicyDefinition).replace()",
    ],
)
def test_the_assignment_detector_sees_every_spelling(tmp_path: Path, source: str) -> None:
    """The four shapes a line-anchored regex misses or misidentifies.

    The auditor's mutation: extra spacing and a qualified target both evaded the
    previous detector, and a bare-name key would let a same-named class in another
    module satisfy the record.
    """
    tree = ast.parse(source)
    targets = [
        ast.unparse(target.value).rsplit(".", 1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "__signature__"
    ]

    assert targets == ["PolicyDefinition"]


def test_a_function_local_assignment_on_a_class_still_hides_fields() -> None:
    """Lexical scope does not establish what the target is.

    The disproof of an earlier, wrong rule: a ``__signature__`` assigned inside a
    function body was assumed to belong to a call-time object, so discovery
    skipped function bodies. A class pinned from inside a function hides exactly
    as much, and a module-level-only scan reports nothing at all.
    """

    @dataclasses.dataclass
    class Sample:
        visible: int = 0
        concealed: int = 0

    def pin_from_inside_a_function() -> None:
        Sample.__signature__ = inspect.signature(Sample).replace(  # type: ignore[attr-defined]
            parameters=[
                parameter
                for name, parameter in inspect.signature(Sample).parameters.items()
                if name != "concealed"
            ]
        )

    assert hidden_fields(Sample) == set()
    pin_from_inside_a_function()
    assert hidden_fields(Sample) == {"concealed"}


def test_discovery_reaches_assignments_nested_in_a_function() -> None:
    """The scan itself, over the shape the module-level rule could not see."""
    source = "def build():\n    Klass.__signature__ = value\n"
    nested = [
        ast.unparse(target.value).rsplit(".", 1)[-1]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "__signature__"
    ]
    module_only = [
        target
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        for target in node.targets
    ]

    assert nested == ["Klass"]
    assert module_only == []


def test_every_non_class_exemption_states_what_it_was_verified_to_be() -> None:
    """An exemption without a reason is the silence this record exists to end."""
    assert NON_CLASS_SIGNATURE_SITES
    for reference, reason in NON_CLASS_SIGNATURE_SITES.items():
        assert ":" in reference, reference
        assert len(reason.strip()) > 40, reference


@pytest.mark.parametrize("reference", sorted(NON_CLASS_SIGNATURE_SITES))
def test_every_non_class_exemption_really_targets_a_non_class(reference: str) -> None:
    """The claim is checked, not taken.

    A prose reason is unfalsifiable on its own: an entry naming a real class with
    a plausible sentence beside it would escape the record entirely, which would
    make this allowlist the category exemption it replaced. So the claim is
    executed. A module-level class resolves by name; a target that does not
    resolve at module level cannot be one, which is what distinguishes a
    function-local wrapper from a pinned class.
    """
    module, _enclosing, name = reference.split(":", 2)
    resolved = getattr(importlib.import_module(module), name, None)

    assert not inspect.isclass(resolved), (
        f"{reference} is exempted as non-class but resolves to the class {resolved!r}; "
        "record its hidden fields in HIDDEN_CONSTRUCTOR_FIELDS instead of exempting it"
    )


def test_the_non_class_check_would_reject_a_class_named_in_the_allowlist() -> None:
    """The detector, fed the escape it exists to refuse."""
    honest = getattr(
        importlib.import_module("zeroth.integrations.langgraph._tool_wrappers"),
        "governed",
        None,
    )
    a_real_class = importlib.import_module("zeroth.governance.policy.models").PolicyDefinition

    assert not inspect.isclass(honest)
    assert inspect.isclass(a_real_class)


def test_the_non_class_allowlist_names_only_sites_that_exist() -> None:
    """A retired exemption would let a real hiding site inherit its pass."""
    stale = sorted(set(NON_CLASS_SIGNATURE_SITES) - set(signature_assignments()))

    assert stale == [], f"exempted but no longer present: {stale}"


def test_the_record_names_no_class_that_hides_nothing() -> None:
    """A stale entry would let a real exclusion hide behind a retired one."""
    empty = sorted(
        reference for reference, fields in HIDDEN_CONSTRUCTOR_FIELDS.items() if not fields
    )

    assert empty == []


def test_the_helper_sees_a_hidden_field_it_was_not_told_about() -> None:
    """The detector, fed a class that hides a field the record does not name.

    Without this the assertions above could pass because ``hidden_fields`` always
    returns the empty set, which is exactly the failure mode this file exists to
    prevent elsewhere.
    """

    @dataclasses.dataclass
    class Sample:
        visible: int = 0
        concealed: int = 0

    assert hidden_fields(Sample) == set()

    Sample.__signature__ = inspect.signature(Sample).replace(  # type: ignore[attr-defined]
        parameters=[
            parameter
            for name, parameter in inspect.signature(Sample).parameters.items()
            if name != "concealed"
        ]
    )

    assert hidden_fields(Sample) == {"concealed"}


def test_the_measured_totals_are_what_the_audit_reported() -> None:
    """A03-14's two named classes, pinned by count so the record cannot be padded."""
    policy = _resolve("zeroth.governance.policy.models:PolicyDefinition")
    tool_call = _resolve("zeroth.governance.audit.models:ToolCallRecord")

    assert len(declared_fields(policy)) == 15
    assert len(inspect.signature(policy).parameters) == 8
    assert len(hidden_fields(tool_call)) == 10


def test_two_assignments_with_the_same_target_name_are_two_sites() -> None:
    """Multiplicity is the property a name-keyed dict destroyed.

    Collapsing by target name meant a second assignment writing the same name
    vanished from the map -- measured as ``assignment_map_size_delta=0`` when the
    reviewer injected one -- and it meant the allowlist exempted a *spelling*
    rather than a place. Keyed by lexical site, the two are distinct.
    """
    source = (
        "class Klass:\n    pass\n\n\n"
        "Klass.__signature__ = a\n\n\n"
        "def build():\n    Klass.__signature__ = b\n"
    )
    sites = _assignment_sites(ast.parse(source), "probe.module")

    assert len(sites) == 2
    assert [site for site, _ in sites] == [
        "probe.module:<module>:Klass",
        "probe.module:build:Klass",
    ]


def test_the_allowlist_exempts_a_place_and_not_a_name() -> None:
    """The same target name in another location is a different, unexempted site."""
    exempted = "zeroth.integrations.langgraph._tool_wrappers:_govern_callable:governed"

    assert exempted in NON_CLASS_SIGNATURE_SITES
    assert "zeroth.integrations.langgraph._tool_wrappers:_elsewhere:governed" not in (
        NON_CLASS_SIGNATURE_SITES
    )
    assert "zeroth.governance.policy.models:_elsewhere:governed" not in (NON_CLASS_SIGNATURE_SITES)
