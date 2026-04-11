"""Block a tool call via policy — example for docs/how-to/cookbook/policy-block.md.

Wires a :class:`PolicyGuard` with a :class:`PolicyDefinition` that denies
``Capability.NETWORK_WRITE`` and evaluates it against a graph node whose
``capability_bindings`` include that capability. The guard returns a
``DENY`` EnforcementResult — the same decision the orchestrator uses at
run time to terminate the run with
``failure_state.reason == "policy_violation"``. Fully in-process.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0

    from zeroth.core.examples.quickstart import build_demo_graph_with_policy
    from zeroth.core.policy import PolicyGuard
    from zeroth.core.policy.models import Capability, PolicyDecision, PolicyDefinition
    from zeroth.core.policy.registry import CapabilityRegistry, PolicyRegistry
    from zeroth.core.runs import Run, RunStatus

    # 1. Build a graph whose tool node requires NETWORK_WRITE.
    graph = build_demo_graph_with_policy(denied_capabilities=[Capability.NETWORK_WRITE])
    tool = next(n for n in graph.nodes if n.node_id == "tool")

    # 2. CapabilityRegistry is NOT self-populating — every Capability value
    #    referenced by a node's capability_bindings must be registered.
    capabilities = CapabilityRegistry()
    for cap in Capability:
        capabilities.register(cap.value, cap)

    # 3. Register a policy that denies NETWORK_WRITE under the id the
    #    quickstart helper binds to the tool node ("block-demo-caps").
    policies = PolicyRegistry()
    policies.register(
        PolicyDefinition(
            policy_id="block-demo-caps",
            denied_capabilities=[Capability.NETWORK_WRITE],
        )
    )

    # 4. Evaluate the guard exactly the way the orchestrator does at run start.
    guard = PolicyGuard(policy_registry=policies, capability_registry=capabilities)
    synthetic_run = Run(
        graph_version_ref=graph.graph_id + "@1",
        deployment_ref="demo",
        status=RunStatus.RUNNING,
    )
    result = guard.evaluate(graph, tool, synthetic_run, {"message": "write to internet"})
    print(f"decision={result.decision.value} reason={result.reason!r}")
    assert result.decision is PolicyDecision.DENY, f"expected DENY, got {result.decision}"
    print("policy-block demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
