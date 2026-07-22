from __future__ import annotations

import pytest

from zeroth.core.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.core.langgraph_gateway.models import AdmissionRequest
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDecision,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
    apply_secret_policy,
)
from zeroth.core.runs import Run


def _graph() -> Graph:
    return Graph(
        graph_id="graph-policy",
        name="policy",
        entry_step="agent",
        policy_bindings=["policy://graph"],
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-policy:v1",
                capability_bindings=["capability://memory-read", "capability://secret-access"],
                policy_bindings=["policy://node"],
                agent=AgentNodeData(
                    instruction="respond",
                    model_provider="provider://demo",
                ),
            )
        ],
        edges=[],
    )


def test_policy_guard_allows_declared_capabilities() -> None:
    capability_registry = CapabilityRegistry()
    capability_registry.register("capability://memory-read", Capability.MEMORY_READ)
    capability_registry.register("capability://secret-access", Capability.SECRET_ACCESS)

    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://graph",
            allowed_capabilities=[Capability.MEMORY_READ, Capability.SECRET_ACCESS],
        )
    )
    policy_registry.register(PolicyDefinition(policy_id="policy://node"))

    guard = PolicyGuard(policy_registry=policy_registry, capability_registry=capability_registry)
    node = _graph().nodes[0]
    decision = guard.evaluate(
        _graph(), node, Run(graph_version_ref="graph-policy:v1", deployment_ref="graph-policy"), {}
    )

    assert decision.decision is PolicyDecision.ALLOW
    assert decision.effective_capabilities == {Capability.MEMORY_READ, Capability.SECRET_ACCESS}


def test_policy_guard_denies_node_when_capability_is_denied() -> None:
    capability_registry = CapabilityRegistry()
    capability_registry.register("capability://memory-read", Capability.MEMORY_READ)
    capability_registry.register("capability://secret-access", Capability.SECRET_ACCESS)

    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://graph",
            allowed_capabilities=[Capability.MEMORY_READ, Capability.SECRET_ACCESS],
        )
    )
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://node",
            denied_capabilities=[Capability.SECRET_ACCESS],
        )
    )

    guard = PolicyGuard(policy_registry=policy_registry, capability_registry=capability_registry)
    node = _graph().nodes[0]
    decision = guard.evaluate(
        _graph(), node, Run(graph_version_ref="graph-policy:v1", deployment_ref="graph-policy"), {}
    )

    assert decision.decision is PolicyDecision.DENY
    assert "secret_access" in decision.reason


def test_apply_secret_policy_filters_environment_by_allowlist() -> None:
    assert apply_secret_policy(
        {"API_KEY": "keep", "OTHER": "drop"},
        allowed_secrets=["API_KEY"],
        secret_access_enabled=True,
    ) == {"API_KEY": "keep"}

    assert (
        apply_secret_policy(
            {"API_KEY": "keep"},
            allowed_secrets=["API_KEY"],
            secret_access_enabled=False,
        )
        == {}
    )


def test_policy_guard_rejects_unknown_capability_ref() -> None:
    guard = PolicyGuard(policy_registry=PolicyRegistry(), capability_registry=CapabilityRegistry())
    node = _graph().nodes[0]

    with pytest.raises(KeyError):
        guard.evaluate(
            _graph(),
            node,
            Run(graph_version_ref="graph-policy:v1", deployment_ref="graph-policy"),
            {},
        )


def _admission_request(**updates: object) -> AdmissionRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "principal_id": "user-1",
        "roles": ("operator", "reader"),
        "deployment_ref": "deployment-a",
        "assistant_id": "assistant-a",
        "operation": "runs.create",
        "input_payload": {"message": "hello"},
        "input_classification": "internal",
        "input_size_bytes": 20,
        "policy_bindings": ("policy://admission",),
    }
    values.update(updates)
    return AdmissionRequest(**values)


def _admission_guard(*policies: PolicyDefinition) -> PolicyGuard:
    registry = PolicyRegistry()
    for policy in policies:
        registry.register(policy)
    return PolicyGuard(policy_registry=registry)


def test_policy_definition_backwards_compatible_dump_excluding_defaults() -> None:
    policy = PolicyDefinition(
        policy_id="policy://legacy",
        allowed_capabilities=[Capability.MEMORY_READ],
    )

    assert policy.model_dump(mode="json", exclude_defaults=True) == {
        "policy_id": "policy://legacy",
        "allowed_capabilities": ["memory_read"],
    }
    assert set(policy.model_dump()) == {
        "policy_id",
        "allowed_capabilities",
        "denied_capabilities",
        "allowed_secrets",
        "network_mode",
        "approval_required_for_side_effects",
        "timeout_override_seconds",
        "sandbox_strictness_mode",
    }


@pytest.mark.parametrize(
    ("constraint", "request_update"),
    [
        ({"allowed_tenants": ["tenant-b"]}, {}),
        ({"allowed_principals": ["user-2"]}, {}),
        ({"allowed_assistants": ["assistant-b"]}, {}),
        ({"allowed_deployments": ["deployment-b"]}, {}),
        ({"allowed_input_classifications": ["public"]}, {}),
        ({"max_input_bytes": 19}, {}),
    ],
)
def test_run_admission_denies_each_unsatisfied_constraint(
    constraint: dict[str, object], request_update: dict[str, object]
) -> None:
    guard = _admission_guard(PolicyDefinition(policy_id="policy://admission", **constraint))

    decision = guard.evaluate_run_admission(_admission_request(**request_update))

    assert decision.allowed is False
    assert decision.reason == "zeroth.policy_denied"
    assert decision.policy_version.startswith("sha256:")


def test_run_admission_requires_every_role_across_bound_policies() -> None:
    policies = (
        PolicyDefinition(policy_id="policy://one", required_roles=["operator"]),
        PolicyDefinition(policy_id="policy://two", required_roles=["auditor"]),
    )
    guard = _admission_guard(*policies)
    request = _admission_request(policy_bindings=("policy://one", "policy://two"))

    denied = guard.evaluate_run_admission(request)
    allowed = guard.evaluate_run_admission(
        request.model_copy(update={"roles": ("reader", "operator", "auditor")})
    )

    assert denied.allowed is False
    assert denied.reason == "zeroth.policy_denied"
    assert allowed.allowed is True
    assert allowed.reason is None


def test_run_admission_intersects_allowlists_and_uses_strictest_size_limit() -> None:
    policies = (
        PolicyDefinition(
            policy_id="policy://one",
            allowed_tenants=["tenant-a", "tenant-b"],
            max_input_bytes=100,
        ),
        PolicyDefinition(
            policy_id="policy://two",
            allowed_tenants=["tenant-a"],
            max_input_bytes=20,
        ),
    )
    guard = _admission_guard(*policies)
    bindings = ("policy://one", "policy://two")

    assert guard.evaluate_run_admission(
        _admission_request(policy_bindings=bindings, input_size_bytes=20)
    ).allowed
    assert not guard.evaluate_run_admission(
        _admission_request(policy_bindings=bindings, tenant_id="tenant-b")
    ).allowed
    assert not guard.evaluate_run_admission(
        _admission_request(policy_bindings=bindings, input_size_bytes=21)
    ).allowed


def test_run_admission_missing_binding_is_typed_safe_denial() -> None:
    decision = _admission_guard().evaluate_run_admission(_admission_request())

    assert decision.allowed is False
    assert decision.reason == "zeroth.policy_unavailable"
    assert decision.policy_version.startswith("sha256:")


def test_run_admission_empty_bindings_allow_with_deterministic_version() -> None:
    guard = _admission_guard()
    request = _admission_request(policy_bindings=())

    first = guard.evaluate_run_admission(request)
    second = guard.evaluate_run_admission(request)

    assert first.allowed is True
    assert first.policy_version == second.policy_version
    assert first.policy_version.startswith("sha256:")


def test_run_admission_policy_version_is_binding_order_independent() -> None:
    policies = (
        PolicyDefinition(policy_id="policy://one", allowed_tenants=["tenant-a"]),
        PolicyDefinition(policy_id="policy://two", required_roles=["operator"]),
    )
    guard = _admission_guard(*policies)

    forward = guard.evaluate_run_admission(
        _admission_request(policy_bindings=("policy://one", "policy://two"))
    )
    reverse = guard.evaluate_run_admission(
        _admission_request(policy_bindings=("policy://two", "policy://one"))
    )

    assert forward.policy_version == reverse.policy_version


def test_run_admission_policy_version_changes_with_admission_constraints() -> None:
    original = _admission_guard(
        PolicyDefinition(policy_id="policy://admission", max_input_bytes=20)
    ).evaluate_run_admission(_admission_request())
    changed = _admission_guard(
        PolicyDefinition(policy_id="policy://admission", max_input_bytes=21)
    ).evaluate_run_admission(_admission_request())

    assert original.policy_version != changed.policy_version


@pytest.mark.parametrize(
    ("first_fields", "second_fields"),
    [
        (
            {"allowed_capabilities": [Capability.MEMORY_READ]},
            {"denied_capabilities": [Capability.MEMORY_READ]},
        ),
        (
            {"network_mode": "deny"},
            {"network_mode": "allow"},
        ),
    ],
)
def test_run_admission_policy_version_hashes_full_resolved_policy_definition(
    first_fields: dict[str, object], second_fields: dict[str, object]
) -> None:
    first = _admission_guard(
        PolicyDefinition(policy_id="policy://admission", **first_fields)
    ).evaluate_run_admission(_admission_request())
    second = _admission_guard(
        PolicyDefinition(policy_id="policy://admission", **second_fields)
    ).evaluate_run_admission(_admission_request())

    assert first.policy_version != second.policy_version


def test_run_admission_policy_version_canonicalizes_full_policy_list_order() -> None:
    first_policy = PolicyDefinition(
        policy_id="policy://admission",
        allowed_capabilities=[Capability.MEMORY_READ, Capability.SECRET_ACCESS],
        allowed_secrets=["B", "A"],
        allowed_tenants=["tenant-b", "tenant-a"],
    )
    reordered_policy = PolicyDefinition(
        policy_id="policy://admission",
        allowed_capabilities=[Capability.SECRET_ACCESS, Capability.MEMORY_READ],
        allowed_secrets=["A", "B"],
        allowed_tenants=["tenant-a", "tenant-b"],
    )

    first = _admission_guard(first_policy).evaluate_run_admission(_admission_request())
    reordered = _admission_guard(reordered_policy).evaluate_run_admission(_admission_request())

    assert first.policy_version == reordered.policy_version
