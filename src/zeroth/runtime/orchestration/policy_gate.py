"""Policy and approval gating for the orchestration runtime.

:class:`RuntimePolicyGate` owns every check that can stop a node before it is
dispatched, plus the resolution of an approval that unblocks one:

* loop guards — the run's step and wall-clock ceilings,
* policy evaluation, both sequential and per fan-out branch,
* the side-effect approval gate policy can require,
* consumption of a resolved side-effect approval on re-entry.

Two of its dependencies are callbacks rather than objects. Failing a run and
refreshing artifact TTLs belong to the driver, so the gate declares that it
needs them instead of reaching back into an orchestrator to find them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import (
    AgentNode,
    ExecutableUnitNode,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
    Node,
)
from zeroth.governance.approvals import ApprovalDecision, ApprovalService
from zeroth.governance.policy import Capability, PolicyDecision, PolicyGuard
from zeroth.governance.policy.errors import parse_effective_capabilities
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.runs import Run
from zeroth.runtime.subgraphs.resolver import base_node_id


@dataclass(frozen=True, slots=True)
class RuntimePolicyGate:
    """Evaluates the guards, policies, and approvals that precede a dispatch."""

    run_repository: Any
    audit_recorder: RuntimeAuditRecorder
    fail_run: Callable[[Run, str, str], Awaitable[Run]]
    refresh_artifact_ttls: Callable[[Run], Awaitable[None]]
    policy_guard: PolicyGuard | None = None
    approval_service: ApprovalService | None = None
    executable_unit_runner: Any = None
    agent_runners: Mapping[str, Any] = field(default_factory=dict)

    async def enforce_loop_guards(
        self,
        graph: Graph,
        run: Run,
        started_at: float,
        *,
        failure_reason: str | None = None,
    ) -> Run | None:
        """Check if the run has exceeded its step or time limits.

        Returns a failed Run if a limit is exceeded, or None if everything
        is within bounds. This prevents infinite loops in graphs.
        """
        total_steps = len(run.execution_history)
        settings = graph.execution_settings
        if total_steps >= settings.max_total_steps:
            return await self.fail_run(
                run,
                failure_reason or "max_total_steps",
                "max total step limit exceeded",
            )
        if settings.max_total_runtime_seconds is not None:
            elapsed = perf_counter() - started_at
            if elapsed > settings.max_total_runtime_seconds:
                return await self.fail_run(
                    run,
                    failure_reason or "max_total_runtime",
                    "max total runtime exceeded",
                )
        return None

    async def enforce_policy(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Check if the policy guard allows this node to run.

        If a policy guard is configured and denies execution, the run is
        marked as failed with a policy violation reason. Returns None if
        no guard is set or if the policy allows execution.
        """
        guard = self.policy_guard
        if guard is None:
            return None
        result = guard.evaluate(graph, node, run, input_payload)
        if result.decision is PolicyDecision.ALLOW:
            enforcement = dict(run.metadata.get("enforcement", {}))
            enforcement[node.node_id] = result.model_dump(mode="json")
            run.metadata["enforcement"] = enforcement
            return None

        # Policy failures are recorded like a node attempt so operators can diagnose why it stopped.
        await self.audit_recorder.record_policy_rejection(
            run,
            node,
            input_payload,
            result.model_dump(mode="json"),
            result.reason,
        )
        run.touch()
        run = await self.run_repository.put(run)
        return await self.fail_run(
            run, "policy_violation", result.reason or "policy denied execution"
        )

    async def enforce_policy_for_branch(
        self,
        graph: Graph,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> str | None:
        """Check policy for a branch node dispatch. Returns denial reason or None."""
        guard = self.policy_guard
        if guard is None:
            return None
        result = guard.evaluate(graph, node, run, input_payload)
        if result.decision is PolicyDecision.ALLOW:
            # G2: persist the granted capability set for this branch node exactly
            # as the sequential ``enforce_policy`` does. Without this the branch
            # dispatch's ``enforcement_context_for`` reads an empty context and
            # ``require_capabilities`` fail-closed DENIES memory reads/writes and
            # capability-bearing tools even when the node correctly declared them.
            #
            # Concurrency: fan-out branches run under ``asyncio.gather``.
            # ``setdefault`` creates the shared ``enforcement`` dict exactly once,
            # then each branch writes ONLY its own ``node_id`` key. The guard
            # ignores per-branch input, so sibling branches evaluating the same
            # node write an identical value — never clobbering each other. There
            # is no ``await`` between the setdefault and the key write, so the
            # read-modify-write is atomic under cooperative scheduling.
            enforcement = run.metadata.setdefault("enforcement", {})
            enforcement[node.node_id] = result.model_dump(mode="json")
            return None
        return result.reason or "policy denied execution"

    def enforcement_context_for(self, run: Run, node_id: str) -> dict[str, Any]:
        """Return the stored policy enforcement context for a node, if any."""
        enforcement = run.metadata.get("enforcement", {})
        if not isinstance(enforcement, Mapping):
            return {}
        context = enforcement.get(node_id, {})
        if not isinstance(context, Mapping):
            return {}
        return dict(context)

    def effective_capabilities_for(self, run: Run, node_id: str) -> set[Capability] | None:
        """Return the node's granted capability set, or None when enforcement is off.

        WS-C: mirrors the runner's rule for the orchestrator's own memory-resolve
        callers (retrieval, template-memory). ``None`` iff the policy guard is not
        wired; otherwise the parsed granted set (empty denies — fail-closed). The
        active/off decision is the explicit ``policy_guard is not None`` check, not
        an inference from missing keys, so an unenforced node can never bypass an
        active gate.
        """
        if self.policy_guard is None:
            return None
        return parse_effective_capabilities(self.enforcement_context_for(run, node_id))

    async def gate_policy_required_side_effects(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Pause execution when policy requires approval before side effects."""
        enforcement = self.enforcement_context_for(run, node.node_id)
        if not enforcement.get("approval_required_for_side_effects"):
            return None
        approved_nodes = set(run.metadata.get("approved_side_effect_nodes", []))
        if node.node_id in approved_nodes:
            return None
        if not self.node_has_side_effects(node):
            return None
        service = self.approval_service
        approval_id = None
        if service is not None:
            approval = await service.create_pending(
                run=run,
                node=HumanApprovalNode(
                    node_id=node.node_id,
                    graph_version_ref=node.graph_version_ref,
                    human_approval=HumanApprovalNodeData(),
                ),
                input_payload=dict(input_payload),
            )
            approval_id = approval.approval_id
        run.status = RunStatus.WAITING_APPROVAL
        payloads = dict(run.metadata.get("node_payloads", {}))
        payloads[node.node_id] = dict(input_payload)
        run.metadata["node_payloads"] = payloads
        run.metadata["pending_approval"] = {
            "node_id": node.node_id,
            "input": dict(input_payload),
            "approval_id": approval_id,
            "kind": "side_effect_policy",
        }
        run.pending_node_ids.insert(0, node.node_id)
        run.touch()
        persisted = await self.run_repository.put(run)
        await self.run_repository.write_checkpoint(persisted)
        await self.refresh_artifact_ttls(persisted)
        return persisted

    async def consume_side_effect_approval(
        self,
        run: Run,
        node: Node,
        input_payload: Mapping[str, Any],
    ) -> Run | None:
        """Resolve pending side-effect approval state before re-executing a node."""
        pending = run.metadata.get("pending_approval")
        if not isinstance(pending, Mapping):
            return None
        if pending.get("kind") != "side_effect_policy" or pending.get("node_id") != node.node_id:
            return None
        approval_id = pending.get("approval_id")
        if approval_id is None or self.approval_service is None:
            run.status = RunStatus.WAITING_APPROVAL
            run.pending_node_ids.insert(0, node.node_id)
            persisted = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(persisted)
            await self.refresh_artifact_ttls(persisted)
            return persisted
        record = await self.approval_service.get(approval_id)
        if record is None or record.resolution is None:
            run.status = RunStatus.WAITING_APPROVAL
            run.pending_node_ids.insert(0, node.node_id)
            persisted = await self.run_repository.put(run)
            await self.run_repository.write_checkpoint(persisted)
            await self.refresh_artifact_ttls(persisted)
            return persisted
        run.metadata.pop("pending_approval", None)
        if record.resolution.decision is ApprovalDecision.REJECT:
            return await self.fail_run(run, "approval_rejected", "approval rejected")
        approved_nodes = set(run.metadata.get("approved_side_effect_nodes", []))
        approved_nodes.add(node.node_id)
        run.metadata["approved_side_effect_nodes"] = sorted(approved_nodes)
        if record.resolution.edited_payload is not None:
            payloads = dict(run.metadata.get("node_payloads", {}))
            payloads[node.node_id] = dict(record.resolution.edited_payload)
            run.metadata["node_payloads"] = payloads
        return None

    def node_has_side_effects(self, node: Node) -> bool:
        """Detect whether a node can cause side effects that require approval."""
        if isinstance(node, ExecutableUnitNode):
            if bool(node.execution_config.get("side_effect")):
                return True
            registry = getattr(self.executable_unit_runner, "registry", None)
            if registry is not None and registry.has(node.executable_unit.manifest_ref):
                return bool(registry.get(node.executable_unit.manifest_ref).manifest.side_effect)
            return False
        if isinstance(node, AgentNode):
            runner = self.agent_runners.get(node.node_id) or self.agent_runners.get(
                base_node_id(node.node_id)
            )
            if runner is None:
                return False
            config = getattr(runner, "config", None)
            attachments = getattr(config, "tool_attachments", []) if config is not None else []
            return any(attachment.side_effect_allowed for attachment in attachments)
        return False
