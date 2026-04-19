from __future__ import annotations

import pytest

from zeroth.core.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
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
