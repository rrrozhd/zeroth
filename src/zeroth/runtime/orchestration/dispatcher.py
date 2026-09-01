"""Node dispatch for the orchestration runtime.

:class:`NodeDispatcher` resolves a node's type and runs it. Agent nodes are the
involved case: before the runner is called the dispatcher may override its
instruction with a rendered template, wrap its provider for cost
instrumentation and cost cascading, inject shared memory/budget services,
attach a context-window tracker, and attach a tool executor — then restore every
one of those in a ``finally``, because a runner that cannot fork is shared
across dispatches and must not leak state from one node into the next.

Executable-unit and retrieval nodes take their own paths; entrypoint nodes pass
their payload through.

All dependencies arrive explicitly. The dispatcher holds no run state, so the
same instance serves the sequential drive loop and every fan-out branch.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from zeroth.contracts.conditions import ConditionEvaluator
from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import (
    AgentNode,
    Condition,
    EntrypointNode,
    ExecutableUnitNode,
    Graph,
    HttpRequestNode,
    IfNode,
    LoopNode,
    Node,
    OperationIdentity,
    RetrievalNode,
    SubgraphNode,
    operation_identity,
)
from zeroth.governance.audit.capture_vocabulary import normalize_reason_code
from zeroth.integrations.http.models import EndpointConfig, HttpCallRecord
from zeroth.platform.dispatch.operations import (
    OperationClaim,
    OperationState,
    SideEffectOperationStore,
)
from zeroth.platform.observability import start_span
from zeroth.runtime.agents import AgentRunner
from zeroth.runtime.orchestration.audit_recorder import enforcement_audit_fields
from zeroth.runtime.orchestration.errors import (
    MemoryBindingResolutionError,
    NodeDispatcherError,
)
from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate
from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor
from zeroth.runtime.parallel.models import BranchContext
from zeroth.runtime.runs import Run
from zeroth.runtime.runs.costs import rollup_run_cost
from zeroth.runtime.subgraphs.resolver import canonical_runner_id

logger = logging.getLogger(__name__)

# Sentinel for "attribute not present" in optional runner wiring.
_MISSING: Any = object()

# These connector implementations execute entirely inside the Zeroth process.
# A completed call therefore proves both zero provider calls and measured zero
# cost. External/vector connectors remain unmeasured here and must obtain their
# identities and settlement from the embedding instrumentation boundary.
_LOCAL_NO_PROVIDER_MEMORY_CONNECTOR_TYPES = frozenset({"ephemeral", "key_value", "thread"})

# Regex for {namespace.field} placeholders supported in binding key / key_prefix.
_KEY_PLACEHOLDER_RE = re.compile(r"\{(input|state|run)\.([^}]+)\}")


def _embedding_cost_audit_fields(records: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    """Aggregate consumed embedding settlements without inventing missing cost."""
    if not records:
        return {}
    measured = 0.0
    estimated = 0.0
    saw_measured = False
    saw_estimated = False
    for record in records:
        if record.get("cleanup_status") != "complete":
            return {}
        measurement = record.get("cost_measurement")
        try:
            if measurement == "measured" and record.get("cost_usd") is not None:
                measured += float(record["cost_usd"])
                saw_measured = True
            elif measurement == "estimated" and record.get("estimated_cost_usd") is not None:
                estimated += float(record["estimated_cost_usd"])
                saw_estimated = True
            else:
                return {}
        except (TypeError, ValueError):
            return {}
    fields: dict[str, Any] = {
        "provider_call_count": len(records),
        "cost_usd": measured if saw_measured else None,
        "estimated_cost_usd": estimated if saw_estimated else None,
        "cost_measurement": "estimated" if saw_estimated else "measured",
    }
    if len(records) == 1:
        fields.update(
            {
                key: records[0].get(key)
                for key in (
                    "operation_id",
                    "cost_event_id",
                    "provider_request_id",
                    "cleanup_status",
                )
                if records[0].get(key) is not None
            }
        )
    return fields


def _flatten_template_variables(
    value: Any,
    *,
    prefix: str,
    output: dict[str, object],
    ancestors: frozenset[int] = frozenset(),
) -> None:
    """Collect scalar render values under their namespace-qualified paths.

    Template audit redaction matches rendered values by the names that supplied
    them. Keeping the full path makes a nested key such as
    ``input.credentials.api_key`` discoverable instead of stopping after the
    first namespace level. Containers are traversed without rendering arbitrary
    mapping keys, and cycles contribute no audit candidate.
    """
    if isinstance(value, Mapping):
        if id(value) in ancestors:
            return
        nested_ancestors = ancestors | {id(value)}
        for key, item in value.items():
            if type(key) is not str:
                continue
            child = f"{prefix}.{key}" if prefix else key
            _flatten_template_variables(
                item,
                prefix=child,
                output=output,
                ancestors=nested_ancestors,
            )
        return
    if isinstance(value, list | tuple):
        if id(value) in ancestors:
            return
        nested_ancestors = ancestors | {id(value)}
        for index, item in enumerate(value):
            _flatten_template_variables(
                item,
                prefix=f"{prefix}[{index}]",
                output=output,
                ancestors=nested_ancestors,
            )
        return
    output[prefix] = value


@dataclass(frozen=True, slots=True)
class SubgraphDispatchResult:
    """Normalized result of dispatching or resuming one child graph."""

    output: dict[str, Any] | None = None
    audit: dict[str, Any] | None = None
    terminal_run: Run | None = None


async def dispatch_subgraph_node(
    *,
    executor: Any,
    orchestrator: Any,
    parent_graph: Graph,
    parent_run: Run,
    node: SubgraphNode,
    input_payload: dict[str, Any],
    branch_context: BranchContext | None,
    step_tracker: Any,
) -> SubgraphDispatchResult:
    """Route a subgraph through its runtime executor with durable pause replay.

    ``step_tracker`` is a required keyword with no default. It used to be absent
    from this signature entirely, so the executor's own permissive
    ``step_tracker=None`` default silently took over and a nested subgraph got a
    fresh step budget instead of consuming the parent's. A caller that genuinely
    has no tracker to share must now pass ``None`` and say why.
    """
    if executor is None or orchestrator is None:
        raise NodeDispatcherError("SubgraphExecutor is required for SubgraphNode dispatch")
    pending = parent_run.metadata.get("pending_subgraph")
    resumed = bool(pending and pending.get("node_id") == node.node_id)
    if resumed:
        child = await executor.resume(
            orchestrator=orchestrator,
            parent_graph=parent_graph,
            parent_run=parent_run,
            paused_child_run_id=pending["child_run_id"],
            branch_index=(branch_context.branch_index if branch_context is not None else None),
            step_tracker=step_tracker,
        )
    else:
        child = await executor.execute(
            orchestrator=orchestrator,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id=node.node_id,
            input_payload=input_payload,
            branch_context=branch_context,
            step_tracker=step_tracker,
        )
    if child.status is RunStatus.WAITING_APPROVAL:
        parent_run.status = RunStatus.WAITING_APPROVAL
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": child.run_id,
            "node_id": node.node_id,
            "graph_ref": node.subgraph.graph_ref,
            "version": node.subgraph.version,
        }
        parent_run.touch()
        persisted = await orchestrator.run_repository.put(parent_run)
        await orchestrator.run_repository.write_checkpoint(persisted)
        return SubgraphDispatchResult(terminal_run=persisted)
    if child.status is not RunStatus.COMPLETED:
        failure = child.failure_state
        detail = failure.message if failure is not None else "unknown failure"
        child_cost = rollup_run_cost(child)
        error = NodeDispatcherError(
            f"child run {child.run_id} ended {child.status.value}: {detail}"
        )
        error.audit_record = {  # type: ignore[attr-defined]
            "subgraph_run_id": child.run_id,
            "subgraph_graph_ref": node.subgraph.graph_ref,
            "subgraph_status": child.status.value,
            "subgraph_resumed": resumed,
            "cost_usd": child_cost.cost_usd,
            "estimated_cost_usd": child_cost.estimated_cost_usd,
            "cost_measurement": child_cost.cost_measurement,
        }
        raise error
    parent_run.metadata.pop("pending_subgraph", None)
    output = child.final_output or {}
    if not isinstance(output, dict):
        output = {"result": output}
    child_cost = rollup_run_cost(child)
    return SubgraphDispatchResult(
        output=output,
        audit={
            "subgraph_run_id": child.run_id,
            "subgraph_graph_ref": node.subgraph.graph_ref,
            "subgraph_status": child.status.value,
            "subgraph_resumed": resumed,
            "cost_usd": child_cost.cost_usd,
            "estimated_cost_usd": child_cost.estimated_cost_usd,
            "cost_measurement": child_cost.cost_measurement,
        },
    )


def substitute_binding_key(
    key: str,
    *,
    input_payload: dict[str, Any],
    state: dict[str, Any],
    run_id: str,
) -> str:
    """Replace ``{input.field}``, ``{state.field}``, ``{run.run_id}`` placeholders in a key.

    Unknown placeholders are left unchanged so callers can detect them.
    """
    sources: dict[str, dict[str, Any]] = {
        "input": input_payload,
        "state": state,
        "run": {"run_id": run_id},
    }

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        namespace, field_name = m.group(1), m.group(2)
        ns = sources.get(namespace, {})
        return str(ns[field_name]) if field_name in ns else m.group(0)

    return _KEY_PLACEHOLDER_RE.sub(_replace, key)


class SideEffectReconciliationExhaustedError(NodeDispatcherError):
    """Raised when an ambiguous operation has spent its reconciliation budget.

    Refusing is deliberate. The alternative -- re-executing anyway -- risks
    applying the effect a second time on an integration that cannot dedupe,
    which is the one outcome worse than stalling and asking a human.
    """


def _operation_audit_fields(
    identity: OperationIdentity,
    claim: OperationClaim,
) -> dict[str, Any]:
    """Flatten one operation's outcome into audit fields.

    First execution, replay suppression and ambiguity are recorded as distinct
    facts rather than one "retried" flag, and the residual duplicate risk is
    carried through so an at-least-once integration stays visible in the record
    instead of being implied away.
    """
    return {
        "operation_key": identity.operation_key,
        "operation_target_ref": identity.target_ref,
        "operation_support": identity.support.value,
        "operation_state": claim.state.value.lower(),
        "operation_first_execution": claim.first_execution,
        "operation_replay_suppressed": (
            not claim.first_execution and claim.state is OperationState.COMPLETED
        ),
        "operation_reconciliation_required": claim.reconciliation_required,
        "operation_reconciliation_exhausted": claim.reconciliation_exhausted,
        "operation_residual_duplicate_risk": claim.residual_duplicate_risk,
    }


def _mark_suppressed_replay(audit: dict[str, Any]) -> None:
    """Record the exact incremental cost of a replay that did no work.

    The original operation keeps its own cost record.  A completed-operation
    replay only reads the durable receipt and does not call the provider or
    external action again, so its incremental cost is a measured zero.  Reusing
    the original cost here would double-count it; leaving the value absent makes
    strict budget enforcement incorrectly treat the recovered run as unknown.
    """
    audit.update(
        operation_replay_suppressed=True,
        cost_usd=0.0,
        estimated_cost_usd=0.0,
        cost_measurement="measured",
    )


@dataclass(frozen=True, slots=True)
class NodeDispatcher:
    """Resolves node types and runs them with their governed wiring applied."""

    agent_runners: Mapping[str, AgentRunner]
    executable_unit_runner: Any
    tool_executor: RuntimeToolExecutor
    policy_gate: RuntimePolicyGate | None = None
    thread_resolver: Any = None
    secret_resolver: Any = None
    memory_resolver: Any = None
    budget_enforcer: Any = None
    regulus_client: Any = None
    cost_estimator: Any = None
    cost_instrumentation: Any = None
    per_run_cap_usd: float | None = None
    deployment_ref: str | None = None
    template_registry: Any = None
    template_renderer: Any = None
    context_window_enabled: bool = True
    # Run-scoped MCP sessions, keyed by run_id and owned by the orchestrator.
    # Keyed rather than held because ``_node_dispatcher`` is a property that
    # rebuilds this object on every access -- there is no per-run instance here
    # to hang a session on, and a session must outlive a single dispatch.
    mcp_pools: Mapping[str, Any] | None = None
    # Optional: without it, side-effecting dispatch behaves exactly as before.
    operation_store: SideEffectOperationStore | None = None
    # Optional callback asking a target what a prior operation did. Absent means
    # the integration cannot be queried -- the residual at-least-once case.
    operation_outcome_lookup: Callable[[OperationIdentity], Awaitable[str | None]] | None = None
    # Provider-free managed HTTP transport. Optional for backward-compatible
    # runtimes, but an authored HttpRequestNode fails closed when it is absent.
    http_client: Any = None

    def _enforcement_context_for(self, run: Run, node_id: str) -> dict[str, Any]:
        if self.policy_gate is None:
            return {}
        return self.policy_gate.enforcement_context_for(run, node_id)

    @staticmethod
    def _campaign_runtime_context(run: Run) -> dict[str, Any]:
        metadata = run.metadata if isinstance(run.metadata, Mapping) else {}
        campaign_id = metadata.get("campaign_id")
        if campaign_id is None:
            return {}
        return {
            "campaign_id": str(campaign_id),
            # A tagged campaign is strict unless it explicitly opts out. The live
            # evaluation never opts out, so a missing accounting hook fails closed.
            "campaign_strict": bool(metadata.get("campaign_strict", True)),
        }

    def _operation_identity_for(
        self,
        run: Run,
        target_ref: str,
        *,
        call_ordinal: int = 0,
        branch_id: str | None = None,
    ) -> OperationIdentity:
        """Derive the logical-operation identity for one side-effecting call.

        The dispatcher is the one place that legitimately reads run state, so it
        resolves the identity here and hands it on explicitly. Everything below
        this line receives it as a parameter and never reaches back into
        ``run.metadata`` -- that indirection is what made the identity invisible
        to integrations before.

        Runs driven outside the token engine carry no dispatch record; they fall
        back to the run id, which still yields one stable key per (run, target,
        call) triple.

        ``branch_id`` is the legacy fan-out's discriminator. Siblings share the
        run object, share that fallback idempotency key (``drive`` never stages a
        token dispatch for a node carrying ``parallel_config``) and share the
        downstream node, so without it N branches derived ONE identity: the first
        to claim owned the operation and every sibling was suppressed as a replay
        of it, receiving the first branch's receipt as its own output. That is
        cross-branch data corruption, not merely a missed effect.

        It widens ``target_ref`` rather than adding a field to the identity
        contract, for the same reason ``tool_executor`` folds the provider call id
        in there: the widened ref is what ``_operation_audit_fields`` already
        publishes as ``operation_target_ref``, so the discriminator is visible in
        the durable audit instead of hidden in unaudited key material.

        The token engine needs none of this -- its per-token dispatch already
        carries a distinct ``idempotency_key`` -- so its dispatches pass no branch
        and keep their existing keys byte-identically.
        """
        dispatch = run.metadata.get("token_dispatch")
        if not isinstance(dispatch, Mapping):
            dispatch = {}
        return operation_identity(
            run_id=run.run_id,
            dispatch_id=str(dispatch.get("dispatch_id") or run.run_id),
            idempotency_key=str(dispatch.get("idempotency_key") or run.run_id),
            attempt=int(dispatch.get("attempt") or 0),
            target_ref=target_ref if branch_id is None else f"{target_ref}#branch:{branch_id}",
            call_ordinal=call_ordinal,
        )

    def _is_side_effect_free(self, node: ExecutableUnitNode) -> bool:
        """Whether this unit declared that it has no side effects.

        Fail-safe by design: an inline unit has no manifest, an unregistered ref
        cannot be inspected, and a runner that does not expose the probe tells us
        nothing -- all of those are treated as side-effecting. Only an explicit
        ``side_effect = False`` on a registered manifest skips the guard.
        """
        if node.executable_unit.inline_source is not None:
            return False
        probe = getattr(self.executable_unit_runner, "declares_side_effect", None)
        if probe is None:
            return False
        try:
            declared = probe(node.executable_unit.manifest_ref)
        except Exception:  # noqa: BLE001 - an unreadable manifest is "unknown"
            return False
        return declared is False

    async def _guarded_side_effect(
        self,
        identity: OperationIdentity,
        invoke: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, dict[str, Any]]:
        """Run one side-effecting invocation under its durable operation record.

        Returns ``(result, audit_fields)``, with ``result`` None when the call was
        suppressed because a previous attempt is known to have succeeded.

        Without a store wired this is a pass-through, so deployments that have
        not opted in keep their existing behaviour exactly (R9).

        A timeout is deliberately *not* treated as a failure: it is the one
        outcome where the effect may well have landed, so it becomes AMBIGUOUS
        and leaves durable reconciliation work behind.
        """
        store = self.operation_store
        if store is None:
            return await invoke(), {}

        claim = await store.claim(
            identity.operation_key,
            run_id=identity.run_id,
            dispatch_id=identity.dispatch_id,
            idempotency_key=identity.idempotency_key,
            target_ref=identity.target_ref,
            attempt=identity.attempt,
            support=identity.support.value,
        )
        audit = _operation_audit_fields(identity, claim)
        if not claim.first_execution:
            if claim.state is OperationState.COMPLETED:
                _mark_suppressed_replay(audit)
                audit["replayed_output"] = json.loads(claim.receipt or "{}")
                return None, audit
            return await self._resolve_ambiguous(identity, claim, invoke, audit)

        result = await self._invoke_checkpointed(identity, invoke)
        # The audit was built from the *claim*, when the operation was still
        # IN_FLIGHT. Leaving it there records every successful side effect as
        # perpetually in flight, so the terminal state is written back.
        audit["operation_state"] = OperationState.COMPLETED.value.lower()
        return result, audit

    async def _invoke_checkpointed(
        self,
        identity: OperationIdentity,
        invoke: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Perform the effect and record its outcome durably.

        Both the first execution and a reconciliation retry go through here. A
        retry that bypassed this left a *successful* re-execution still marked
        AMBIGUOUS, so the very next attempt would reconcile it all over again.

        A timeout is deliberately not folded into the failure branch: it is the
        one outcome where the effect may well have landed.
        """
        store = self.operation_store
        assert store is not None  # only reached from the guarded path
        try:
            result = await invoke()
        except TimeoutError as error:
            await store.mark_ambiguous(identity.operation_key, reason=str(error) or "timeout")
            # Carry the operation facts on the exception: re-raising discards the
            # audit dict this method never returns, and a timeout is exactly the
            # outcome whose record matters most.
            error.operation_audit = {  # type: ignore[attr-defined]
                "operation_key": identity.operation_key,
                "operation_state": OperationState.AMBIGUOUS.value.lower(),
                "operation_residual_duplicate_risk": not identity.dedupe_supported,
            }
            raise
        except Exception as error:
            await store.fail(identity.operation_key, error=str(error))
            error.operation_audit = {  # type: ignore[attr-defined]
                "operation_key": identity.operation_key,
                "operation_state": OperationState.FAILED.value.lower(),
            }
            raise
        await store.complete(
            identity.operation_key,
            receipt=json.dumps(result.output_data, default=str, sort_keys=True),
        )
        return result

    async def _resolve_ambiguous(
        self,
        identity: OperationIdentity,
        claim: OperationClaim,
        invoke: Callable[[], Awaitable[Any]],
        audit: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        """Consult the reconciliation path before considering re-execution.

        Blind re-execution is what this whole subsystem exists to avoid, so the
        outcome lookup runs *first*. The budget is a real stop: once it is spent
        the operation is refused rather than re-executed, because the runtime
        still does not know whether the effect landed and guessing would be the
        one failure mode worse than stalling.
        """
        store = self.operation_store
        assert store is not None  # only reached from the guarded path
        if claim.reconciliation_exhausted:
            audit["operation_reconciliation_exhausted"] = True
            raise SideEffectReconciliationExhaustedError(
                f"operation {identity.operation_key} is ambiguous and its "
                "reconciliation budget is spent; re-executing could apply the "
                "effect twice"
            )

        if not await store.begin_outcome_lookup(identity.operation_key):
            settled = await store.get(identity.operation_key)
            if settled is not None and settled["state"] == OperationState.COMPLETED:
                receipt = settled.get("receipt")
                audit["operation_state"] = OperationState.COMPLETED.value.lower()
                _mark_suppressed_replay(audit)
                audit["replayed_output"] = json.loads(receipt or "{}")
                return None, audit
            audit["operation_reconciliation_exhausted"] = True
            raise SideEffectReconciliationExhaustedError(
                f"operation {identity.operation_key} remains ambiguous and requires "
                "an authorized operator resolution; it will not be re-executed"
            )

        receipt: str | None = None
        error: str | None = None
        if self.operation_outcome_lookup is not None:
            try:
                receipt = await self.operation_outcome_lookup(identity)
            except Exception as exc:  # noqa: BLE001 - a failed lookup is data
                error = f"outcome lookup failed: {exc}"
        else:
            # No lookup means the integration cannot be asked what happened.
            # That is exactly the residual at-least-once case, recorded rather
            # than implied away.
            error = "integration exposes no outcome lookup"

        state = await store.finish_outcome_lookup(
            identity.operation_key,
            receipt=receipt,
            error=error,
        )
        audit["operation_state"] = state.value.lower()
        if state is OperationState.COMPLETED:
            # COMPLETED is COMPLETED no matter who discovered it. When a
            # competing reconciler settled the record while the local lookup
            # came back empty, the local receipt is None -- but re-executing a
            # confirmed effect is exactly the double-apply this path exists to
            # prevent, so the stored receipt is fetched rather than guessed.
            if receipt is None:
                stored = await store.get(identity.operation_key)
                receipt = None if stored is None else stored.get("receipt")
            _mark_suppressed_replay(audit)
            audit["replayed_output"] = json.loads(receipt or "{}")
            return None, audit

        audit["operation_reconciliation_exhausted"] = True
        raise SideEffectReconciliationExhaustedError(
            f"operation {identity.operation_key} remains ambiguous and requires "
            "an authorized operator resolution; it will not be re-executed"
        )

    def _effective_capabilities_for(self, run: Run, node_id: str) -> Any:
        if self.policy_gate is None:
            return None
        return self.policy_gate.effective_capabilities_for(run, node_id)

    async def dispatch(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None = None,
        *,
        branch_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dispatch a node inside an OBS tracing span.

        Wraps every dispatch path (main drive loop and fan-out branches, which
        call this directly) so each node hop produces one span carrying the
        node/run identifiers that also key the metrics and audit records.
        ``graph`` enables tool-attachment dispatch for agents with tool
        bindings; callers without it simply run the agent tool-less.

        ``branch_id`` names the parallel branch this dispatch belongs to. It is
        the only thing distinguishing sibling fan-out dispatches of the same
        node, which otherwise share every field of the operation identity; the
        sequential and token-engine callers pass none and are unaffected.
        """
        with start_span(
            "zeroth.node",
            {
                "zeroth.node_id": node.node_id,
                "zeroth.node_type": type(node).__name__,
                "zeroth.run_id": run.run_id,
            },
        ):
            return await self.dispatch_inner(node, run, input_payload, graph, branch_id=branch_id)

    async def dispatch_inner(
        self,
        node: Node,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None = None,
        *,
        branch_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run a single node and return its output and audit data.

        Figures out what kind of node it is (agent or executable unit),
        finds the right runner, and executes it. Raises NodeDispatcherError
        if the node type isn't supported or no runner is registered.
        """
        if isinstance(node, AgentNode):
            return await self._dispatch_agent(node, run, input_payload, graph, branch_id)
        if isinstance(node, EntrypointNode):
            # Ingress pass-through: POST /v1/runs already validated the payload
            # against the deployment's pinned entry contract. The entrypoint
            # marks where (and with what) the run entered the workflow.
            return dict(input_payload), {
                "execution_mode": "entrypoint",
                "passthrough": True,
                "cost_usd": 0.0,
                "estimated_cost_usd": 0.0,
                "cost_measurement": "measured",
            }
        if isinstance(node, LoopNode):
            return self._dispatch_loop(node, run, input_payload)
        if isinstance(node, IfNode):
            return self._dispatch_if(node, run, input_payload)
        if isinstance(node, ExecutableUnitNode):
            return await self._dispatch_executable_unit(node, run, input_payload, branch_id)
        if isinstance(node, RetrievalNode):
            return await self.dispatch_retrieval(node, run, input_payload)
        if isinstance(node, HttpRequestNode):
            return await self._dispatch_http_request(node, run, input_payload)
        raise NodeDispatcherError(f"unsupported node type: {type(node)!r}")

    @staticmethod
    def _http_audit_record(
        record: HttpCallRecord,
        *,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        """Return content-free HTTP facts that survive metadata-only capture."""
        reason_code = normalize_reason_code(type(error).__name__) if error is not None else None
        nested = record.model_dump(mode="json")
        # Transport exception text is content and can include peer-controlled
        # material. The typed outcome code is sufficient for durable evidence.
        nested["error"] = reason_code
        audit: dict[str, Any] = {
            "execution_mode": "resilient_http_get",
            "node_kind": "http_request",
            "http": nested,
            "target_url_sha256": hashlib.sha256(record.url.encode("utf-8")).hexdigest(),
            "retry_count": record.retry_count,
            "duration_ms": record.latency_ms,
            "cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "cost_measurement": "measured",
            "provider_call_count": 0,
        }
        if record.status_code is not None:
            audit["upstream_status_code"] = record.status_code
        if reason_code is not None:
            audit["reason_code"] = reason_code
        return audit

    async def _dispatch_http_request(
        self,
        node: HttpRequestNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Execute one bounded GET and expose no response headers in evidence."""
        data = node.http_request
        if not data.url:
            raise NodeDispatcherError("HTTP request URL is required")
        if self.http_client is None:
            raise NodeDispatcherError(
                f"HTTP request node '{node.node_id}' requires the resilient HTTP client"
            )
        config = EndpointConfig(
            max_retries=data.max_retries,
            retryable_status_codes=set(data.retryable_status_codes),
            timeout=data.timeout_seconds,
        )
        try:
            observed = await self.http_client.request_with_record(
                "GET",
                data.url,
                endpoint_config=config,
                effective_capabilities=self._effective_capabilities_for(run, node.node_id),
            )
        except Exception as error:
            record = getattr(error, "http_call_record", None)
            if isinstance(record, HttpCallRecord):
                error.audit_record = self._http_audit_record(  # type: ignore[attr-defined]
                    record,
                    error=error,
                )
            raise

        response = observed.response
        record = observed.call_record
        if len(response.content) > data.max_response_bytes:
            error = NodeDispatcherError(f"HTTP response exceeded {data.max_response_bytes} bytes")
            error.audit_record = self._http_audit_record(record, error=error)  # type: ignore[attr-defined]
            raise error

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not response.content:
            body: Any = None
        elif content_type == "application/json" or content_type.endswith("+json"):
            try:
                body = response.json()
            except ValueError as exc:
                error = NodeDispatcherError("HTTP response declared malformed JSON")
                error.audit_record = self._http_audit_record(  # type: ignore[attr-defined]
                    record,
                    error=error,
                )
                raise error from exc
        else:
            body = response.text

        output = {
            **dict(input_payload),
            "http_response": {
                "status_code": response.status_code,
                "content_type": content_type or None,
                "body": body,
            },
        }
        return output, self._http_audit_record(record)

    def _dispatch_loop(
        self,
        node: LoopNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Select repeat/done/limit without invoking an external runner."""
        if not node.loop.until.strip():
            raise NodeDispatcherError("Loop condition is required")
        completed_visits = run.node_visit_counts.get(node.node_id, 0)
        condition_met = False
        if completed_visits > 0:
            condition_met = (
                ConditionEvaluator()
                .evaluate(
                    Condition(expression=node.loop.until),
                    {
                        "payload": dict(input_payload),
                        "state": run.model_dump(mode="json"),
                        "node_visit_counts": dict(run.node_visit_counts),
                    },
                    condition_id=f"{node.node_id}:until",
                    source_node_id=node.node_id,
                )
                .matched
            )

        if completed_visits == 0:
            route = "repeat"
            retries_used = 0
            termination_reason = None
        elif condition_met:
            route = "done"
            retries_used = min(completed_visits, node.loop.max_retries)
            termination_reason = "condition_met"
        elif completed_visits <= node.loop.max_retries:
            route = "repeat"
            retries_used = completed_visits
            termination_reason = None
        else:
            route = "limit"
            retries_used = node.loop.max_retries
            termination_reason = "max_retries_exhausted"

        output = dict(input_payload)
        loop_states = dict(output.get("zeroth_loop", {}))
        loop_states[node.node_id] = {
            "route": route,
            "attempt": 1 + retries_used,
            "retries_used": retries_used,
            "max_retries": node.loop.max_retries,
            "termination_reason": termination_reason,
        }
        output["zeroth_loop"] = loop_states
        return output, {
            "execution_mode": "loop_control",
            "route": route,
            "attempt": 1 + retries_used,
            "retries_used": retries_used,
            "max_retries": node.loop.max_retries,
            "termination_reason": termination_reason,
            "cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "cost_measurement": "measured",
        }

    def _dispatch_if(
        self,
        node: IfNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Evaluate one expression and expose one deterministic named route."""
        result = (
            ConditionEvaluator()
            .evaluate(
                Condition(expression=node.condition.expression),
                {
                    "payload": dict(input_payload),
                    "state": run.model_dump(mode="json"),
                    "node_visit_counts": dict(run.node_visit_counts),
                },
                condition_id=f"{node.node_id}:expression",
                source_node_id=node.node_id,
            )
        )
        value = result.details["value"]
        if node.condition.routes:
            selected = next(
                (
                    candidate
                    for candidate in node.condition.routes
                    if not candidate.is_default
                    and type(candidate.match_value) is type(value)
                    and candidate.match_value == value
                ),
                next(candidate for candidate in node.condition.routes if candidate.is_default),
            )
            route = selected.route_id
            decision = {"route": route, "value": value}
        else:
            route = "true" if result.matched else "false"
            decision = {"route": route, "matched": result.matched}
        output = dict(input_payload)
        decision_states = dict(output.get("zeroth_if", {}))
        decision_states[node.node_id] = decision
        output["zeroth_if"] = decision_states
        return output, {
            "execution_mode": "if_control",
            "condition_id": f"{node.node_id}:expression",
            "expression_sha256": hashlib.sha256(
                node.condition.expression.encode("utf-8")
            ).hexdigest(),
            "route": route,
            **({"value": value} if node.condition.routes else {"matched": result.matched}),
            "cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "cost_measurement": "measured",
        }

    async def _dispatch_agent(
        self,
        node: AgentNode,
        run: Run,
        input_payload: Mapping[str, Any],
        graph: Graph | None,
        branch_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # Parallel child-workflow ids carry an ephemeral branch prefix. Keep the
        # subgraph-qualified portion: bootstrap registers child templates under
        # that canonical key so authored ids cannot collide across deployments.
        prototype = self.agent_runners.get(node.node_id) or self.agent_runners.get(
            canonical_runner_id(node.node_id)
        )
        if prototype is None:
            raise NodeDispatcherError(f"no agent runner registered for {node.node_id}")
        declared_fork = inspect.getattr_static(prototype, "fork_for_dispatch", _MISSING)
        fork_for_dispatch = prototype.fork_for_dispatch if declared_fork is not _MISSING else None
        runner = fork_for_dispatch() if callable(fork_for_dispatch) else prototype

        # Resolve thread before template rendering so memory can use thread scope.
        thread_id = await self.resolve_thread(node, run)
        tmb_audit_records: list[dict[str, Any]] = []

        # Phase 36: Template resolution -- resolve and render before agent execution.
        effective_instruction: str | None = None
        rendered_prompt_for_audit: str | None = None
        template_ref_for_audit: dict[str, Any] | None = None
        agent_template_ref = getattr(node.agent, "template_ref", None)
        if (
            self.template_registry is not None
            and self.template_renderer is not None
            and agent_template_ref is not None
        ):
            from zeroth.contracts.templates import TemplateRegistry, TemplateRenderer

            template_ref = node.agent.template_ref
            registry: TemplateRegistry = self.template_registry
            renderer: TemplateRenderer = self.template_renderer
            template_candidate = registry.get(template_ref.name, template_ref.version)
            template = (
                await template_candidate
                if inspect.isawaitable(template_candidate)
                else template_candidate
            )
            # Resolve template memory bindings before building render_vars.
            _memory_ns, _tmb_records = await self.resolve_template_memory(
                node, run, thread_id, input_payload
            )
            tmb_audit_records.extend(_tmb_records)
            render_vars: dict[str, Any] = {
                "input": dict(input_payload),
                "state": dict(run.metadata) if run.metadata else {},
                "memory": _memory_ns,
            }
            render_result = renderer.render(template, render_vars)
            effective_instruction = render_result.rendered
            rendered_prompt_for_audit = render_result.rendered

            # Phase 36: Redact secret variable values before audit storage.
            from zeroth.contracts.templates.redaction import (
                identify_secret_variables,
                redact_rendered_prompt,
            )

            # Flatten every nested leaf for redaction matching. The namespace
            # remains in the path so secret-bearing parents (for example
            # ``input.credentials``) also taint their descendants.
            render_vars_flat: dict[str, object] = {}
            _flatten_template_variables(
                render_vars,
                prefix="",
                output=render_vars_flat,
            )
            secret_vars = identify_secret_variables(
                list(render_vars_flat.keys()),
            )
            if secret_vars:
                rendered_prompt_for_audit = redact_rendered_prompt(
                    render_result.rendered,
                    render_vars_flat,
                    secret_vars,
                )

            template_ref_for_audit = {
                "name": template.name,
                "version": template.version,
            }

        # Capture every dispatch-mutable surface before the first assignment.
        # Production runners are forks, while lightweight protocol-less test
        # doubles use the prototype and therefore require exception-safe cleanup.
        original_config = getattr(runner, "config", _MISSING)
        original_provider = getattr(runner, "provider", _MISSING)
        original_memory_resolver = getattr(runner, "memory_resolver", _MISSING)
        original_budget_enforcer = getattr(runner, "budget_enforcer", _MISSING)
        original_context_tracker = getattr(runner, "context_tracker", _MISSING)
        original_tool_executor = getattr(runner, "tool_executor", _MISSING)
        _context_window_audit = None
        try:
            # Phase 36: Override runner config instruction with rendered template.
            if effective_instruction is not None and original_config is not _MISSING:
                runner.config = original_config.model_copy(
                    update={"instruction": effective_instruction}
                )

            # Phase 18: Wrap provider with cost instrumentation (per ECON-01).
            # Use getattr so lightweight runners without .provider still work.
            if original_provider is not _MISSING and self.cost_estimator is not None:
                try:
                    from zeroth.econ.analytics.adapter import InstrumentedProviderAdapter

                    tenant_id = run.tenant_id or "default"
                    runner.provider = InstrumentedProviderAdapter(
                        inner=original_provider,
                        regulus_client=self.regulus_client,
                        cost_estimator=self.cost_estimator,
                        node_id=node.node_id,
                        run_id=run.run_id,
                        tenant_id=tenant_id,
                        deployment_ref=self.deployment_ref or "unknown",
                        workflow_version=run.graph_version_ref,
                        subject_id=(
                            str(run.metadata["subject_id"])
                            if run.metadata.get("subject_id") is not None
                            else None
                        ),
                        # Persistent reservation is an admission-control boundary,
                        # not a passive analytics decorator. Preserve legacy
                        # unbounded runs when no campaign or local cap requested
                        # strict admission; tagged campaigns still fail closed if
                        # their required cap is missing.
                        cost_instrumentation=(
                            self.cost_instrumentation
                            if self.per_run_cap_usd is not None
                            or run.metadata.get("campaign_id") is not None
                            else None
                        ),
                        campaign_id=(
                            str(run.metadata["campaign_id"])
                            if run.metadata.get("campaign_id") is not None
                            else None
                        ),
                        per_run_cap_usd=self.per_run_cap_usd,
                        branch_id=branch_id,
                    )
                except ImportError:
                    pass

            # Cost cascade wraps the instrumented provider so each attempt is priced.
            agent_data = getattr(node, "agent", None)
            if (
                original_provider is not _MISSING
                and agent_data is not None
                and getattr(agent_data, "cascade_enabled", False)
                and getattr(agent_data, "cheap_model", None)
                and getattr(agent_data, "criticality", "medium") == "low"
            ):
                from zeroth.runtime.agents.cascade import CascadingProviderAdapter

                runner.provider = CascadingProviderAdapter(
                    inner=runner.provider,
                    cheap_model=agent_data.cheap_model,
                )

            # Phase 20: Add shared services only when the runner has none configured.
            if (
                self.memory_resolver is not None
                and original_memory_resolver is not _MISSING
                and original_memory_resolver is None
            ):
                runner.memory_resolver = self.memory_resolver
            if (
                self.budget_enforcer is not None
                and original_budget_enforcer is not _MISSING
                and original_budget_enforcer is None
            ):
                runner.budget_enforcer = self.budget_enforcer

            # Phase 37: Context window tracker injection (per D-09, D-11).
            if (
                self.context_window_enabled
                and original_context_tracker is not _MISSING
                and original_context_tracker is None
                and hasattr(node.agent, "context_window")
                and node.agent.context_window is not None
            ):
                from zeroth.runtime.context import (
                    ContextWindowTracker,
                    LLMSummarizationStrategy,
                    ObservationMaskingStrategy,
                    TruncationStrategy,
                )

                cw_settings = node.agent.context_window
                strategy_name = cw_settings.compaction_strategy
                if strategy_name == "truncation":
                    strategy = TruncationStrategy()
                elif strategy_name == "llm_summarization":
                    strategy = LLMSummarizationStrategy(provider=runner.provider)
                else:
                    strategy = ObservationMaskingStrategy()
                runner.context_tracker = ContextWindowTracker(
                    settings=cw_settings,
                    strategy=strategy,
                )

            enforcement_context = self._enforcement_context_for(run, node.node_id)
            if (
                graph is not None
                and original_tool_executor is not _MISSING
                and original_tool_executor is None
                and getattr(node.agent, "tool_bindings", None)
            ):
                runner.tool_executor = self.tool_executor.build(
                    graph,
                    enforcement_context,
                    operation_identity_factory=lambda target_ref, ordinal: (
                        # The branch belongs in here too: two branches running the
                        # same agent node and calling the same tool replay the same
                        # provider call id, so the keyed_ref alone collides.
                        self._operation_identity_for(
                            run,
                            target_ref,
                            call_ordinal=ordinal,
                            branch_id=branch_id,
                        )
                    ),
                    operation_guard=self._guarded_side_effect,
                    side_effect_free=self._is_side_effect_free,
                    # Resolved per run: the pool is run-scoped, while this
                    # dispatcher is rebuilt on every access, so it cannot hold
                    # the session itself.
                    mcp_pool=self.mcp_pools.get(run.run_id) if self.mcp_pools else None,
                    # The AGENT's id, which is what it has always been -- the
                    # name says so now, because the pool's other subject is the
                    # mcp_tool node and the executor resolves that one itself.
                    mcp_agent_node_id=node.node_id,
                    mcp_effective_capabilities=self._effective_capabilities_for(
                        run, node.node_id
                    ),
                )

            # Budget and capability enforcement are dispatch- and tenant-local.
            runner_context = dict(enforcement_context)
            runner_context.setdefault("tenant_id", run.tenant_id)
            runner_context["capability_enforcement_active"] = (
                self.policy_gate is not None and self.policy_gate.policy_guard is not None
            )
            result = await self.run_agent_with_optional_enforcement(
                runner,
                input_payload,
                thread_id=thread_id,
                runtime_context={
                    "node_id": node.node_id,
                    "run_id": run.run_id,
                    # WS-B: memory resolution is fail-closed on tenant; the
                    # runner forwards this dict unchanged to _load/_store.
                    "tenant_id": run.tenant_id,
                    **self._campaign_runtime_context(run),
                },
                enforcement_context=runner_context,
            )
        finally:
            # Phase 37: Record context window state in audit before restoring.
            _ctx_tracker = getattr(runner, "context_tracker", None)
            if _ctx_tracker is not None and hasattr(_ctx_tracker, "state"):
                _cw_state = _ctx_tracker.state
                # Store for audit enrichment after the finally block.
                _context_window_audit = {
                    "accumulated_tokens": _cw_state.accumulated_tokens,
                    "compaction_count": _cw_state.compaction_count,
                }
            else:
                _context_window_audit = None
            # Restore originals even when setup failed before agent execution.
            if original_config is not _MISSING:
                runner.config = original_config
            if original_provider is not _MISSING:
                runner.provider = original_provider
            if original_memory_resolver is not _MISSING:
                runner.memory_resolver = original_memory_resolver
            if original_budget_enforcer is not _MISSING:
                runner.budget_enforcer = original_budget_enforcer
            # Phase 37: Restore original context tracker.
            if original_context_tracker is not _MISSING:
                runner.context_tracker = original_context_tracker
            if original_tool_executor is not _MISSING:
                runner.tool_executor = original_tool_executor

        audit_record = dict(result.audit_record)
        if enforcement_context:
            # The nested context is kept for the content-capture posture; the
            # flattened fields are what survive the metadata-only default.
            audit_record["enforcement"] = enforcement_context
            audit_record.update(enforcement_audit_fields(enforcement_context, applied=True))
        # Phase 36: Record template metadata in audit.
        if rendered_prompt_for_audit is not None:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["rendered_prompt"] = rendered_prompt_for_audit
            # The raw rendered prompt remains subject to the deployment's
            # content-capture posture. Its digest is safe structural evidence
            # and survives the default metadata-only boundary.
            audit_record["rendered_prompt_sha256"] = hashlib.sha256(
                rendered_prompt_for_audit.encode("utf-8")
            ).hexdigest()
        if template_ref_for_audit is not None:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["template_ref"] = template_ref_for_audit
            audit_record["template_name_sha256"] = hashlib.sha256(
                str(template_ref_for_audit["name"]).encode("utf-8")
            ).hexdigest()
            audit_record["template_version"] = int(template_ref_for_audit["version"])
        # Phase 37: Record context window state in audit.
        if _context_window_audit is not None:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["context_window"] = _context_window_audit
        # Record template memory binding resolution in audit.
        if tmb_audit_records:
            audit_record.setdefault("execution_metadata", {})
            audit_record["execution_metadata"]["template_memory_bindings"] = tmb_audit_records
        return result.output_data, audit_record

    async def _dispatch_executable_unit(
        self,
        node: ExecutableUnitNode,
        run: Run,
        input_payload: Mapping[str, Any],
        branch_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        enforcement_context = self._enforcement_context_for(run, node.node_id)
        if (
            self.secret_resolver is not None
            and getattr(
                self.executable_unit_runner,
                "secret_resolver",
                None,
            )
            is None
        ):
            self.executable_unit_runner.secret_resolver = self.secret_resolver
        inline = node.executable_unit.inline_source is not None
        target_ref = f"node://{node.node_id}" if inline else node.executable_unit.manifest_ref
        identity = self._operation_identity_for(run, target_ref, branch_id=branch_id)

        async def _invoke() -> Any:
            if inline:
                return await self.tool_executor.run_inline(
                    node,
                    input_payload,
                    enforcement_context=enforcement_context,
                    operation_identity=identity,
                )
            return await self.tool_executor.run_unit(
                node.executable_unit.manifest_ref,
                input_payload,
                enforcement_context=enforcement_context,
                timeout_seconds=node.executable_unit.timeout_seconds,
                operation_identity=identity,
            )

        if self._is_side_effect_free(node):
            # R9: a unit that declares no side effect keeps its previous
            # behaviour exactly -- no operation record, no suppression.
            result = await _invoke()
            return result.output_data, dict(result.audit_record)

        result, operation_audit = await self._guarded_side_effect(identity, _invoke)
        if result is None:
            # The operation was suppressed as a replay; the stored receipt is the
            # answer, so the effect is not applied a second time.
            return operation_audit.pop("replayed_output", {}), operation_audit
        audit_record = dict(result.audit_record)
        if inline:
            # Inline Studio code runs in Zeroth's sandbox and has no provider or
            # billable connector boundary.  Mark that known absence explicitly:
            # strict budget admission must distinguish measured zero from an
            # unknown external-unit cost.
            audit_record.update(
                cost_usd=0.0,
                estimated_cost_usd=0.0,
                cost_measurement="measured",
            )
        audit_record.update(operation_audit)
        if enforcement_context:
            # The nested context is kept for the content-capture posture; the
            # flattened fields are what survive the metadata-only default.
            audit_record["enforcement"] = enforcement_context
            audit_record.update(enforcement_audit_fields(enforcement_context, applied=True))
        return result.output_data, audit_record

    async def dispatch_retrieval(
        self,
        node: RetrievalNode,
        run: Run,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Retrieve grounded context from a vector connector for a RetrievalNode (RAG-01).

        Queries the configured memory connector with the input's query field and
        returns the input augmented with the retrieved chunks under ``as_name``.
        The audit record carries the query and per-chunk source attribution
        (ids + metadata), not the chunk bodies (RAG-03).
        """
        data = node.retrieval
        if self.memory_resolver is None:
            raise NodeDispatcherError(
                f"retrieval node '{node.node_id}' requires a memory resolver to be wired"
            )
        query_text = input_payload.get(data.query_key)
        if not isinstance(query_text, str) or not query_text.strip():
            raise NodeDispatcherError(
                f"retrieval node '{node.node_id}': input field '{data.query_key}' "
                "must be a non-empty string"
            )
        from zeroth.contracts.governed import MemoryScope

        scope = {
            "run": MemoryScope.RUN,
            "thread": MemoryScope.THREAD,
            "shared": MemoryScope.SHARED,
        }[data.scope]
        try:
            resolved = await self.memory_resolver.resolve(
                [data.connector_ref],
                thread_id=run.thread_id or None,
                runtime_context={
                    "run_id": run.run_id,
                    "node_id": node.node_id,
                    "tenant_id": run.tenant_id,  # WS-B: fail-closed tenant scoping
                    **self._campaign_runtime_context(run),
                },
                node_id=node.node_id,
                # WS-C: retrieval reads memory -> gated on MEMORY_READ.
                effective_capabilities=self._effective_capabilities_for(run, node.node_id),
            )
        except KeyError as exc:
            raise NodeDispatcherError(
                f"retrieval node '{node.node_id}': unknown memory connector '{data.connector_ref}'"
            ) from exc
        binding = resolved[0]
        connector = binding.connector
        entries = await connector.search({"text": query_text, "limit": data.top_k}, scope)
        embedding_costs: tuple[Mapping[str, Any], ...] = ()
        campaign_context = self._campaign_runtime_context(run)
        consumer = getattr(self.memory_resolver, "consume_embedding_call_costs", None)
        if campaign_context and callable(consumer):
            embedding_costs = await consumer(
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                node_id=node.node_id,
                campaign_id=campaign_context["campaign_id"],
                operation="search",
            )
        chunks = [
            {"id": entry.key, "content": entry.value, "metadata": dict(entry.metadata)}
            for entry in entries
        ]
        output_data = {**dict(input_payload), data.as_name: chunks}
        audit_record = {
            "retrieval_result_count": len(chunks),
            "retrieval": {
                "connector_ref": data.connector_ref,
                "query": query_text,
                "scope": data.scope,
                "top_k": data.top_k,
                "result_count": len(chunks),
                "sources": [
                    {"id": entry.key, "metadata": dict(entry.metadata)} for entry in entries
                ],
            },
        }
        audit_record.update(_embedding_cost_audit_fields(embedding_costs))
        if (
            binding.manifest.connector_type in _LOCAL_NO_PROVIDER_MEMORY_CONNECTOR_TYPES
            or binding.manifest.config.get("provider_call_mode") == "none"
        ):
            # Set this only after search returns. An exception or cancellation
            # must not be relabelled as a completed measured-zero operation.
            audit_record.update(
                cost_usd=0.0,
                estimated_cost_usd=0.0,
                cost_measurement="measured",
                provider_call_count=0,
            )
        return output_data, audit_record

    async def resolve_thread(self, node: AgentNode, run: Run) -> str | None:
        """Figure out which thread ID an agent node should use.

        Some agents participate in threads (conversations), others don't.
        This checks the agent's configuration and uses the thread resolver
        to find or create the right thread.
        """
        mode = node.agent.thread_participation
        persistence_mode = node.agent.state_persistence.get("mode")
        # Persistent conversations live in thread state, so opting in counts
        # as thread participation even when the mode was left at "none".
        persists_conversation = getattr(node.agent, "persist_conversation", False)
        if mode == "none" and persistence_mode != "thread" and not persists_conversation:
            return None
        if self.thread_resolver is not None:
            resolution = await self.thread_resolver.resolve(
                run.thread_id,
                graph_version_ref=run.graph_version_ref,
                deployment_ref=run.deployment_ref,
                participating_agent_refs=[node.node_id],
                run_id=run.run_id,
            )
            run.thread_id = resolution.thread.thread_id
        return run.thread_id

    async def resolve_template_memory(
        self,
        node: AgentNode,
        run: Run,
        thread_id: str | None,
        input_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch memory values declared in ``template_memory_bindings``.

        Returns ``(memory_namespace, audit_records)`` where *memory_namespace*
        is the dict that populates ``{{ memory.* }}`` in prompt templates and
        *audit_records* is a list of per-binding audit dicts appended to the
        node audit record under ``execution_metadata.template_memory_bindings``.

        Raises ``MemoryBindingResolutionError`` when a connector is unknown or
        when a read/search call fails.
        """
        bindings = node.agent.template_memory_bindings
        if not bindings or self.memory_resolver is None:
            return {}, []

        from zeroth.contracts.governed import MemoryScope

        _scope_map = {
            "run": MemoryScope.RUN,
            "thread": MemoryScope.THREAD,
            "shared": MemoryScope.SHARED,
        }

        # Only resolve the connector refs actually used by template bindings.
        refs_needed = list({b.connector_instance_id for b in bindings})
        runtime_context: dict[str, Any] = {
            "node_id": node.node_id,
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,  # WS-B: fail-closed tenant scoping
            **self._campaign_runtime_context(run),
        }
        try:
            resolved = await self.memory_resolver.resolve(
                refs_needed,
                thread_id=thread_id,
                runtime_context=runtime_context,
                node_id=node.node_id,
                # WS-C: template memory bindings read memory -> gated on MEMORY_READ.
                effective_capabilities=self._effective_capabilities_for(run, node.node_id),
            )
        except KeyError as exc:
            raise MemoryBindingResolutionError(
                f"unknown memory connector referenced in template_memory_bindings: {exc}"
            ) from exc

        connector_by_ref: dict[str, Any] = {rb.memory_ref: rb.connector for rb in resolved}
        state: dict[str, Any] = dict(run.metadata) if run.metadata else {}
        input_dict: dict[str, Any] = dict(input_payload)

        memory_ns: dict[str, Any] = {}
        audit_records: list[dict[str, Any]] = []

        for binding in bindings:
            connector = connector_by_ref.get(binding.connector_instance_id)
            if connector is None:
                raise MemoryBindingResolutionError(
                    f"connector '{binding.connector_instance_id}' not found in memory_refs; "
                    "add it to agent.memory_refs before using it in template_memory_bindings"
                )

            scope = _scope_map[binding.scope]

            try:
                if binding.access_mode == "get":
                    resolved_key = substitute_binding_key(
                        binding.key or "",
                        input_payload=input_dict,
                        state=state,
                        run_id=run.run_id,
                    )
                    entry = await connector.read(resolved_key, scope)
                    value = entry.value if entry is not None else binding.default
                    audit_records.append(
                        {
                            "as_name": binding.as_name,
                            "connector_instance_id": binding.connector_instance_id,
                            "access_mode": "get",
                            "key": resolved_key,
                            "scope": binding.scope,
                            "found": entry is not None,
                        }
                    )
                else:  # scan
                    prefix = binding.key_prefix or ""
                    if prefix:
                        prefix = substitute_binding_key(
                            prefix,
                            input_payload=input_dict,
                            state=state,
                            run_id=run.run_id,
                        )
                    all_entries = await connector.search({}, scope)
                    items: dict[str, Any] = {
                        entry.key[len(prefix) :]: entry.value
                        for entry in all_entries
                        if entry.key.startswith(prefix)
                    }
                    if binding.max_items is not None:
                        items = dict(list(items.items())[: binding.max_items])
                    value = items if items else binding.default
                    audit_records.append(
                        {
                            "as_name": binding.as_name,
                            "connector_instance_id": binding.connector_instance_id,
                            "access_mode": "scan",
                            "key_prefix": prefix,
                            "scope": binding.scope,
                            "item_count": len(items),
                        }
                    )
            except MemoryBindingResolutionError:
                raise
            except Exception as exc:
                raise MemoryBindingResolutionError(
                    f"failed to read memory binding '{binding.as_name}' "
                    f"(connector={binding.connector_instance_id}): {exc}"
                ) from exc

            memory_ns[binding.as_name] = value

        return memory_ns, audit_records

    async def run_agent_with_optional_enforcement(
        self,
        runner: AgentRunner,
        input_payload: Mapping[str, Any],
        *,
        thread_id: str | None,
        runtime_context: Mapping[str, Any],
        enforcement_context: Mapping[str, Any],
    ) -> Any:
        """Call agent runners with enforcement context when their signature supports it."""
        parameters = inspect.signature(runner.run).parameters
        if "enforcement_context" in parameters:
            return await runner.run(
                input_payload,
                thread_id=thread_id,
                runtime_context=runtime_context,
                enforcement_context=enforcement_context,
            )
        return await runner.run(
            input_payload,
            thread_id=thread_id,
            runtime_context=runtime_context,
        )
