"""The logical-operation identity carried across every side-effecting boundary.

ZER-26 R1/R2/R10. These tests pin the two properties the whole idempotency story
rests on -- the key is *stable* under every kind of replay and *distinct* between
logical operations -- plus the honesty requirement that an integration which
cannot dedupe says so rather than implying a guarantee it does not provide.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.contracts.graph import (
    OperationIdentity,
    SideEffectSupport,
    derive_operation_key,
    operation_identity,
)


def _identity(**overrides: object) -> OperationIdentity:
    kwargs: dict[str, object] = {
        "run_id": "run_1",
        "dispatch_id": "dsp_abc",
        "idempotency_key": "idem_abc",
        "attempt": 0,
        "target_ref": "unit://charge-card",
    }
    kwargs.update(overrides)
    return operation_identity(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R2 -- stability
# ---------------------------------------------------------------------------


def test_operation_key_is_stable_across_retry_attempts() -> None:
    """A transport retry is the same logical operation, so the key must not move.

    ``attempt`` is carried for observability but deliberately excluded from the
    key material; if it leaked in, every retry would look like new work and the
    downstream dedupe would never fire.
    """
    first = _identity(attempt=0)
    retried = _identity(attempt=3)

    assert first.operation_key == retried.operation_key
    assert first.attempt != retried.attempt


def test_operation_key_is_stable_across_worker_recovery() -> None:
    """Recovery re-derives identity from durable fields only.

    A recovering worker knows the run, the idempotency key and the target; it
    does not know the crashed worker's process-local state. Deriving from those
    three plus the ordinal is what makes recovery reproduce the same key.
    """
    before_crash = _identity()
    after_recovery = operation_identity(
        run_id="run_1",
        dispatch_id="dsp_abc",
        idempotency_key="idem_abc",
        attempt=1,
        target_ref="unit://charge-card",
    )

    assert before_crash.operation_key == after_recovery.operation_key


def test_derivation_ignores_dispatch_id() -> None:
    """``recover_dispatch`` keeps the idempotency key but may re-issue a dispatch.

    Pinning this explicitly: the key is a function of the *logical* operation, so
    a new dispatch id for the same idempotency key must not fork the identity.
    """
    original = _identity(dispatch_id="dsp_abc")
    reissued = _identity(dispatch_id="dsp_zzz")

    assert original.operation_key == reissued.operation_key


# ---------------------------------------------------------------------------
# R2 -- distinctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "run_2"),
        ("idempotency_key", "idem_other"),
        ("target_ref", "unit://refund-card"),
        ("call_ordinal", 1),
    ],
)
def test_distinct_logical_operations_get_distinct_keys(field: str, value: object) -> None:
    """Every dimension that makes an operation logically different must split the key."""
    baseline = _identity()
    other = _identity(**{field: value})

    assert baseline.operation_key != other.operation_key


def test_two_tool_calls_in_one_dispatch_are_distinct_operations() -> None:
    """An agent calling two tools in one dispatch performs two side effects.

    They share run, dispatch and idempotency key, so the call ordinal is the only
    thing keeping them apart -- without it the second call would be suppressed as
    a duplicate of the first.
    """
    first_call = _identity(target_ref="unit://charge-card", call_ordinal=0)
    second_call = _identity(target_ref="unit://charge-card", call_ordinal=1)

    assert first_call.operation_key != second_call.operation_key


# ---------------------------------------------------------------------------
# R1 -- the key cannot be forged
# ---------------------------------------------------------------------------


def test_operation_key_must_match_its_material() -> None:
    """A hand-built identity whose key contradicts its fields is rejected.

    The record is keyed by ``operation_key``; accepting an inconsistent one would
    let a caller collide two unrelated operations onto a single stored outcome.
    """
    with pytest.raises(ValidationError):
        OperationIdentity(
            run_id="run_1",
            dispatch_id="dsp_abc",
            idempotency_key="idem_abc",
            attempt=0,
            target_ref="unit://charge-card",
            call_ordinal=0,
            support=SideEffectSupport.AT_LEAST_ONCE,
            operation_key="op_deadbeefdeadbeefdeadbeef",
        )


def test_derive_operation_key_is_pure() -> None:
    """Same material in, same key out -- no clock, no randomness, no counter."""
    material = {
        "run_id": "run_1",
        "idempotency_key": "idem_abc",
        "target_ref": "unit://charge-card",
        "call_ordinal": 0,
    }

    assert derive_operation_key(**material) == derive_operation_key(**material)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R10 -- residual guarantee is explicit
# ---------------------------------------------------------------------------


def test_support_defaults_to_at_least_once() -> None:
    """Silence must mean the weaker guarantee, not the stronger one.

    An integration that never declared idempotency support gets
    ``AT_LEAST_ONCE``, so the residual duplicate risk is visible by default
    instead of being implied away.
    """
    assert _identity().support is SideEffectSupport.AT_LEAST_ONCE


def test_support_does_not_affect_the_key() -> None:
    """Declaring support changes the guarantee, not the operation's identity.

    Otherwise upgrading an integration to idempotent would orphan every
    in-flight operation record written under the old declaration.
    """
    weak = _identity(support=SideEffectSupport.AT_LEAST_ONCE)
    strong = _identity(support=SideEffectSupport.IDEMPOTENT)

    assert weak.operation_key == strong.operation_key


def test_at_least_once_is_reported_as_unsupported_dedupe() -> None:
    """The record exposes a single predicate callers can branch on."""
    assert _identity(support=SideEffectSupport.AT_LEAST_ONCE).dedupe_supported is False
    assert _identity(support=SideEffectSupport.IDEMPOTENT).dedupe_supported is True
    assert _identity(support=SideEffectSupport.OUTCOME_QUERYABLE).dedupe_supported is True


# ---------------------------------------------------------------------------
# R1 -- propagation to every side-effecting boundary
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """Captures the kwargs each unit-invocation path actually passes through."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        manifest_ref,
        input_payload,
        *,
        enforcement_context=None,
        operation_identity=None,
    ):
        self.calls.append(
            {"path": "run", "ref": manifest_ref, "operation_identity": operation_identity}
        )
        return _Result()

    async def run_inline_source(
        self,
        node_id,
        source,
        input_payload,
        *,
        timeout_seconds=None,
        enforcement_context=None,
        operation_identity=None,
    ):
        self.calls.append(
            {
                "path": "run_inline_source",
                "ref": node_id,
                "operation_identity": operation_identity,
            }
        )
        return _Result()


class _Result:
    output_data = {"ok": True}
    audit_record: dict[str, object] = {}


class _GraphOf:
    """Minimal stand-in: the executor only looks nodes up by id."""

    def __init__(self, *nodes: object) -> None:
        self.nodes = list(nodes)
        self.edges: list[object] = []


def _executor(runner: _RecordingRunner):
    from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor

    return RuntimeToolExecutor(executable_unit_runner=runner)


def test_run_unit_propagates_the_operation_identity() -> None:
    """The manifest-ref path is a side-effecting boundary and must carry identity."""
    import asyncio

    runner = _RecordingRunner()
    identity = _identity(target_ref="unit://charge-card")

    asyncio.run(
        _executor(runner).run_unit(
            "unit://charge-card",
            {},
            enforcement_context={},
            operation_identity=identity,
        )
    )

    assert runner.calls[0]["operation_identity"] is identity


def test_run_inline_propagates_the_operation_identity() -> None:
    """The Studio code-node path is the same boundary and gets the same treatment."""
    import asyncio

    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    runner = _RecordingRunner()
    identity = _identity(target_ref="node://code-1")
    node = ExecutableUnitNode(
        node_id="code-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            inline_source="def handler(x):\n    return x\n",
            execution_mode="inline",
        ),
    )

    asyncio.run(
        _executor(runner).run_inline(
            node,
            {},
            enforcement_context={},
            operation_identity=identity,
        )
    )

    assert runner.calls[0]["operation_identity"] is identity


def test_a_runner_without_the_parameter_still_works() -> None:
    """R9: a third-party runner that never opted in keeps working unchanged.

    The executor already capability-sniffs ``enforcement_context`` this way; the
    identity follows the same rule so adding it cannot break existing runners.
    """
    import asyncio

    class _OldRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, manifest_ref, input_payload):
            self.calls += 1
            return _Result()

    runner = _OldRunner()
    asyncio.run(
        _executor(runner).run_unit(
            "unit://legacy",
            {},
            enforcement_context={},
            operation_identity=_identity(),
        )
    )

    assert runner.calls == 1


def test_agent_tool_calls_get_distinct_identities_per_call() -> None:
    """Two tool calls in one agent turn are two operations, not one repeat.

    ``build`` takes a factory rather than a fixed identity precisely so the call
    ordinal advances; a single shared identity would make the second call look
    like a duplicate of the first and get suppressed.
    """
    import asyncio

    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    runner = _RecordingRunner()
    node = ExecutableUnitNode(
        node_id="tool-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://send-email",
            execution_mode="wrapped_command",
        ),
    )
    graph = _GraphOf(node)

    def _factory(target_ref: str, ordinal: int):
        return operation_identity(
            run_id="run_1",
            dispatch_id="dsp_abc",
            idempotency_key="idem_abc",
            attempt=0,
            target_ref=target_ref,
            call_ordinal=ordinal,
        )

    execute = _executor(runner).build(graph, {}, operation_identity_factory=_factory)

    class _Binding:
        alias = "send_email"
        executable_unit_ref = "node://tool-1"

    asyncio.run(execute(_Binding(), {}))
    asyncio.run(execute(_Binding(), {}))

    keys = [call["operation_identity"].operation_key for call in runner.calls]
    assert len(keys) == 2
    assert keys[0] != keys[1]


# ---------------------------------------------------------------------------
# ZER26-AUD-006: the production runner must declare and consume the identity
# ---------------------------------------------------------------------------


def test_the_runner_protocol_declares_the_identity_by_exact_name() -> None:
    """Capability sniffing matches on the exact parameter name.

    A runner that does not declare `operation_identity` silently never receives
    it -- which is exactly how the identity went missing on the live path while
    the executor seam's own tests passed.
    """
    import inspect

    from zeroth.runtime.orchestration.protocols import ExecutableUnitRunner

    for method in ("run", "run_binding", "run_inline_source"):
        params = inspect.signature(getattr(ExecutableUnitRunner, method)).parameters
        assert "operation_identity" in params, f"{method} must declare operation_identity"


def test_the_concrete_runner_declares_the_identity_by_exact_name() -> None:
    """The protocol is worthless here if the shipped runner does not match it."""
    import inspect

    from zeroth.integrations.execution.runner import ExecutableUnitRunner as ConcreteRunner

    for method in ("run", "run_manifest_ref", "run_binding", "run_inline_source"):
        params = inspect.signature(getattr(ConcreteRunner, method)).parameters
        assert "operation_identity" in params, f"{method} must declare operation_identity"


def test_the_executor_actually_passes_it_to_the_concrete_runner_signature() -> None:
    """Close the loop: sniffing against the real runner selects the identity.

    Asserting the two facts separately would still allow a mismatch; this checks
    the executor's own filter against the shipped runner rather than a fake.
    """
    import inspect

    from zeroth.integrations.execution.runner import ExecutableUnitRunner as ConcreteRunner
    from zeroth.runtime.orchestration.tool_executor import _supported_kwargs

    selected = _supported_kwargs(
        inspect.signature(ConcreteRunner.run).parameters,
        enforcement_context={},
        operation_identity=_identity(),
    )

    assert set(selected) == {"enforcement_context", "operation_identity"}


def test_the_tool_call_id_makes_distinct_calls_distinct_after_recovery() -> None:
    """The ordinal alone restarts; the provider's call id does not.

    `build()` uses a process-local counter, so a rebuilt executor numbers from
    zero again. After recovery a *different* first call to the same target
    therefore inherited the previous call's key and could be suppressed as a
    duplicate — silently dropping real work, which is worse than missing a
    suppression. Two executors are built here precisely to reproduce that reset.
    """
    import asyncio

    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    node = ExecutableUnitNode(
        node_id="tool-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://send-email",
            execution_mode="wrapped_command",
        ),
    )
    graph = _GraphOf(node)

    def _factory(target_ref: str, ordinal: int):
        return operation_identity(
            run_id="run_1",
            dispatch_id="dsp_abc",
            idempotency_key="idem_abc",
            attempt=0,
            target_ref=target_ref,
            call_ordinal=ordinal,
        )

    class _Binding:
        alias = "send_email"
        executable_unit_ref = "node://tool-1"

    def _key_for(call_id):
        runner = _RecordingRunner()
        execute = _executor(runner).build(graph, {}, operation_identity_factory=_factory)
        asyncio.run(execute(_Binding(), {}, call_id))
        return runner.calls[0]["operation_identity"].operation_key

    # Both are the FIRST call of a freshly built executor, so both get ordinal 0.
    first_turn = _key_for("call_aaa")
    after_recovery = _key_for("call_bbb")
    replay_of_first = _key_for("call_aaa")

    assert first_turn != after_recovery, (
        "a different call must not inherit the previous call's identity"
    )
    assert first_turn == replay_of_first, "an exact replay must still be recognised"


def test_a_non_first_replay_after_recovery_is_still_recognised() -> None:
    """ZER26-AUD-007: with a call id present, the ordinal must not be key material.

    The earlier proof compared only *first* calls, forcing ordinal 0 on both
    sides of the recovery. A call that was the agent's third call before the
    crash replays as the recovered executor's first call — same provider call
    id, different ordinal — and mixing the ordinal into the key minted a new
    identity for it, so the replay was not suppressed.
    """
    import asyncio

    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    node = ExecutableUnitNode(
        node_id="tool-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://send-email",
            execution_mode="wrapped_command",
        ),
    )
    graph = _GraphOf(node)

    def _factory(target_ref: str, ordinal: int):
        return operation_identity(
            run_id="run_1",
            dispatch_id="dsp_abc",
            idempotency_key="idem_abc",
            attempt=0,
            target_ref=target_ref,
            call_ordinal=ordinal,
        )

    class _Binding:
        alias = "send_email"
        executable_unit_ref = "node://tool-1"

    def _keys_for(call_ids):
        runner = _RecordingRunner()
        execute = _executor(runner).build(graph, {}, operation_identity_factory=_factory)
        for call_id in call_ids:
            asyncio.run(execute(_Binding(), {}, call_id))
        return [call["operation_identity"].operation_key for call in runner.calls]

    # Before the crash: call_ccc is the agent's THIRD call.
    before_crash = _keys_for(["call_aaa", "call_bbb", "call_ccc"])
    # After recovery: the same call replays as the new executor's FIRST call.
    (after_recovery,) = _keys_for(["call_ccc"])

    assert after_recovery == before_crash[2], (
        "the same provider call id must key the same operation at any position"
    )
    assert len(set(before_crash)) == 3, "distinct calls must still be distinct"


def test_a_reused_call_id_with_different_arguments_stays_distinct() -> None:
    """ZER26-AUD-007: the provider contract does not promise unique call ids.

    A provider that reuses an id across two same-target calls with different
    arguments is describing two different logical operations; keying by the id
    alone gave the second call the first call's key, so real work was wrongly
    suppressed. The argument digest splits them — while a reused id with
    IDENTICAL arguments is indistinguishable from a retry, which is exactly
    when suppression is correct.
    """
    import asyncio

    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    node = ExecutableUnitNode(
        node_id="tool-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://send-email",
            execution_mode="wrapped_command",
        ),
    )
    graph = _GraphOf(node)

    def _factory(target_ref: str, ordinal: int):
        return operation_identity(
            run_id="run_1",
            dispatch_id="dsp_abc",
            idempotency_key="idem_abc",
            attempt=0,
            target_ref=target_ref,
            call_ordinal=ordinal,
        )

    class _Binding:
        alias = "send_email"
        executable_unit_ref = "node://tool-1"

    def _key_for(call_id, arguments):
        runner = _RecordingRunner()
        execute = _executor(runner).build(graph, {}, operation_identity_factory=_factory)
        asyncio.run(execute(_Binding(), arguments, call_id))
        return runner.calls[0]["operation_identity"].operation_key

    first = _key_for("call_reused", {"to": "a@example.com"})
    reused_different = _key_for("call_reused", {"to": "b@example.com"})
    reused_identical = _key_for("call_reused", {"to": "a@example.com"})

    assert first != reused_different, (
        "a reused id with different arguments is a different logical operation"
    )
    assert first == reused_identical, "a reused id with identical arguments is a retry"


def test_an_mcp_tool_call_is_marked_as_outside_the_guarantee() -> None:
    """MCP calls bypass the side-effect boundary, so the record must say so.

    An unmarked audit record reads as though the operation guarantee applied. It
    does not: an MCP tool is not a graph node, never reaches
    RuntimeToolExecutor, and therefore has no identity, no suppression and no
    reconciliation. The negative control matters as much as the positive one —
    a non-MCP call must NOT carry the marker, or the marker means nothing.
    """
    from zeroth.runtime.agents.tools import ToolAttachmentBinding, ToolAttachmentBridge

    bridge = ToolAttachmentBridge()
    mcp = ToolAttachmentBinding(alias="search", executable_unit_ref="mcp://server/search")
    unit = ToolAttachmentBinding(alias="charge", executable_unit_ref="node://charge")

    marked = bridge.build_call_audit(binding=mcp, arguments={}, at_least_once=True)
    unmarked = bridge.build_call_audit(binding=unit, arguments={})

    assert marked["operation_support"] == "at_least_once"
    assert marked["operation_residual_duplicate_risk"] is True
    assert "operation_support" not in unmarked
    assert "operation_residual_duplicate_risk" not in unmarked


def test_the_marker_survives_promotion_to_the_typed_tool_call_record() -> None:
    """ZER26-AUD-006: emitting the marker is not the same as persisting it.

    The typed ``tool_calls`` column is the queryable durable record, and its
    promotion previously rebuilt each entry without the operation fields — so
    every MCP call read, durably, as though the guarantee applied. The negative
    control matters equally: an unmarked call must stay unmarked.
    """
    from zeroth.runtime.agents.tools import ToolAttachmentBinding, ToolAttachmentBridge
    from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder

    bridge = ToolAttachmentBridge()
    mcp = ToolAttachmentBinding(alias="search", executable_unit_ref="mcp://server/search")
    unit = ToolAttachmentBinding(alias="charge", executable_unit_ref="node://charge")
    record = {
        "extra": {
            "tool_calls": [
                bridge.build_call_audit(binding=mcp, arguments={}, at_least_once=True),
                bridge.build_call_audit(binding=unit, arguments={}),
            ]
        }
    }

    tool_calls, _memory = RuntimeAuditRecorder.typed_fields(record)

    marked, unmarked = tool_calls
    assert marked.operation_support == "at_least_once"
    assert marked.operation_residual_duplicate_risk is True
    assert unmarked.operation_support is None
    assert unmarked.operation_residual_duplicate_risk is None


# The failure-path marker proof lives in tests/test_runner_mcp_wiring.py, where
# the AgentRunner._resolve_tool_calls harness exists — it drives the real loop
# with a raising MCP manager rather than calling build_call_audit directly.


# ---------------------------------------------------------------------------
# ZER-49 A06-1 / AC1 -- concurrent fan-out siblings are distinct operations
# ---------------------------------------------------------------------------


class _MemoryOperationStore:
    """The claim/settle semantics of ``SideEffectOperationStore``, in memory.

    Only the transitions ``_guarded_side_effect`` actually drives are modelled.
    ``claim`` is synchronous under the hood, so within one event loop it is
    atomic — which is exactly the first-claim-wins behaviour the real store gets
    from ``insert_if_absent ... RETURNING``.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.claims: list[dict[str, object]] = []

    async def claim(self, operation_key: str, **fields: object):
        from zeroth.platform.dispatch.operations import OperationClaim, OperationState

        self.claims.append({"operation_key": operation_key, **fields})
        row = self.rows.get(operation_key)
        if row is None:
            self.rows[operation_key] = {
                "state": OperationState.IN_FLIGHT,
                "receipt": None,
                **fields,
            }
            return OperationClaim(state=OperationState.IN_FLIGHT, first_execution=True)
        state = row["state"]
        return OperationClaim(
            state=state,  # type: ignore[arg-type]
            first_execution=False,
            receipt=row["receipt"],  # type: ignore[arg-type]
            reconciliation_required=state is OperationState.IN_FLIGHT,
        )

    async def complete(self, operation_key: str, *, receipt: str) -> bool:
        from zeroth.platform.dispatch.operations import OperationState

        row = self.rows[operation_key]
        if row["state"] is OperationState.COMPLETED:
            return False
        row["state"] = OperationState.COMPLETED
        row["receipt"] = receipt
        return True

    async def fail(self, operation_key: str, *, error: str) -> None:
        from zeroth.platform.dispatch.operations import OperationState

        self.rows[operation_key]["state"] = OperationState.FAILED

    async def mark_ambiguous(self, operation_key: str, *, reason: str) -> None:
        from zeroth.platform.dispatch.operations import OperationState

        self.rows[operation_key]["state"] = OperationState.AMBIGUOUS

    async def get(self, operation_key: str):
        return self.rows.get(operation_key)

    @property
    def operation_keys(self) -> list[str]:
        return [str(claim["operation_key"]) for claim in self.claims]


class _CountingUnitRunner:
    """An integration that records how often the effect was actually applied."""

    def __init__(self) -> None:
        self.applications = 0

    async def run(
        self,
        manifest_ref,
        input_payload,
        *,
        enforcement_context=None,
        operation_identity=None,
    ):
        self.applications += 1
        return _CountingResult({"charged": dict(input_payload), "seq": self.applications})


class _CountingResult:
    def __init__(self, output: dict) -> None:
        self.output_data = output
        self.audit_record: dict = {}


class _ExecutionSettings:
    max_total_steps = 100


class _FanOutGraph:
    """Minimal graph: the fan-out path only looks nodes up and reads settings."""

    def __init__(self, *nodes: object) -> None:
        self.nodes = list(nodes)
        self.edges: list[object] = []
        self.execution_settings = _ExecutionSettings()


class _NullRunRepository:
    async def put(self, run):
        return run

    async def write_checkpoint(self, run):
        return "cp"


async def _no_ttls(run) -> None:
    return None


def _charge_unit_node():
    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    return ExecutableUnitNode(
        node_id="charge",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://charge-card",
            execution_mode="wrapped_command",
        ),
    )


def _fan_out_source():
    class _Source:
        node_id = "split"
        node_type = "agent"
        node_version = 1

    return _Source()


def _fan_out_run():
    from zeroth.runtime.runs import Run

    # A legacy (non-token) run: driver.py never stages ``token_dispatch`` for a
    # node carrying ``parallel_config``, so the identity falls back to run_id.
    return Run(run_id="run-fanout-1", graph_version_ref="g:v1", deployment_ref="d")


def _fan_out_executor(dispatcher):
    from zeroth.runtime.orchestration import RuntimeParallelExecutor

    return RuntimeParallelExecutor(
        run_repository=_NullRunRepository(),
        refresh_artifact_ttls=_no_ttls,
        node_dispatcher=dispatcher,
        plan_next_nodes=lambda graph, run, node_id, output: ["charge"],
    )


def _guarded_dispatcher(store, runner):
    from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
    from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor

    return NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=RuntimeToolExecutor(executable_unit_runner=runner),
        operation_store=store,
    )


async def _run_two_branch_fan_out(store, runner):
    from zeroth.runtime.parallel.models import ParallelConfig

    graph = _FanOutGraph(_charge_unit_node())
    run = _fan_out_run()
    source = _fan_out_source()
    output_data = {"items": [{"amount": 10}, {"amount": 20}]}

    return await _fan_out_executor(_guarded_dispatcher(store, runner)).execute_fan_out(
        graph,
        run,
        source,
        "split",
        {},
        output_data,
        {},
        ParallelConfig(split_path="items"),
    )


async def test_concurrent_fan_out_siblings_get_distinct_operation_identities() -> None:
    """AC1: N branches dispatching one downstream node are N operations.

    Siblings share the run, share the fallback idempotency key (a parallel node
    is never staged as a token dispatch) and share the target node, so nothing
    in the derivation material told them apart. The first branch to claim was
    recorded as the operation and every sibling was suppressed as a *replay of
    it* -- handed branch 1's receipt as its own output. This is silent
    cross-branch data corruption, which is strictly worse than a duplicate
    effect, so the identities must differ.
    """
    store = _MemoryOperationStore()
    runner = _CountingUnitRunner()

    fan_in = await _run_two_branch_fan_out(store, runner)

    keys = store.operation_keys
    assert len(keys) == 2, "each branch must claim its own operation"
    assert len(set(keys)) == 2, f"sibling branches minted the same identity: {keys}"
    # The corruption proof: each branch must carry its OWN effect, not a sibling's.
    assert runner.applications == 2
    outputs = [result.output for result in fan_in.results]
    assert [out["charged"] for out in outputs] == [{"amount": 10}, {"amount": 20}]


async def test_a_replayed_fan_out_reproduces_each_branch_its_own_identity() -> None:
    """Distinctness is only half the property: the branch key must also be stable.

    A discriminator that moved between runs would make siblings distinct and
    simultaneously destroy the guarantee the key exists for -- every recovery
    would re-derive fresh keys, claim them as first executions and re-apply
    every branch's effect. ``branch_id`` is ``run_id:branch:index``, so
    re-splitting the same fan-out output reproduces it; this measures that
    rather than trusting the derivation.
    """
    store = _MemoryOperationStore()
    runner = _CountingUnitRunner()

    await _run_two_branch_fan_out(store, runner)
    first_pass = list(store.operation_keys)

    replayed = await _run_two_branch_fan_out(store, runner)

    assert store.operation_keys[2:] == first_pass, "recovery must re-derive the same keys"
    assert runner.applications == 2, "the replay must be suppressed, not re-applied"
    # Each branch must be handed back ITS OWN receipt -- the assertion that pins
    # stability and the discriminator at the same time.
    assert [result.output["charged"] for result in replayed.results] == [
        {"amount": 10},
        {"amount": 20},
    ]


async def test_a_fan_out_branch_agent_tool_call_is_distinct_per_branch() -> None:
    """The same collision reached through an agent's tool call, not a unit node.

    ``_dispatch_agent`` builds the tool executor with a factory closed over the
    run only, so two branches running the same agent node and calling the same
    tool with the same arguments derive one identity between them -- and the
    provider's call id does not help, because it is the same replayed call id
    on both sides.
    """
    from zeroth.contracts.graph import AgentNode, AgentNodeData, AgentToolBinding

    store = _MemoryOperationStore()
    runner = _CountingUnitRunner()

    class _ToolCallingAgentRunner:
        """A runner whose turn makes exactly one tool call."""

        def __init__(self) -> None:
            self.tool_executor = None
            self.config = None
            self.provider = None
            self.memory_resolver = None
            self.budget_enforcer = None
            self.context_tracker = None

        async def run(self, input_payload, *, thread_id=None, runtime_context=None):
            class _Binding:
                alias = "charge_card"
                executable_unit_ref = "node://charge"

            # A replay of the same turn replays the same provider call id.
            await self.tool_executor(_Binding(), {"amount": 10}, "call-charge-1")
            return _CountingResult({"done": True})

    agent_node = AgentNode(
        node_id="agent",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        agent=AgentNodeData(
            instruction="charge it",
            model_provider="test:model",
            tool_bindings=[
                AgentToolBinding(
                    target_node_id="charge",
                    name="charge_card",
                    description="charge the card",
                )
            ],
        ),
    )
    dispatcher = _guarded_dispatcher(store, runner)
    dispatcher = dispatcher.__class__(
        agent_runners={"agent": _ToolCallingAgentRunner()},
        executable_unit_runner=runner,
        tool_executor=dispatcher.tool_executor,
        operation_store=store,
    )
    graph = _FanOutGraph(agent_node, _charge_unit_node())
    run = _fan_out_run()

    from zeroth.runtime.parallel.models import ParallelConfig

    executor = _fan_out_executor(dispatcher)
    executor = executor.__class__(
        run_repository=_NullRunRepository(),
        refresh_artifact_ttls=_no_ttls,
        node_dispatcher=dispatcher,
        plan_next_nodes=lambda graph, run, node_id, output: ["agent"],
    )
    await executor.execute_fan_out(
        graph,
        run,
        _fan_out_source(),
        "split",
        {},
        {"items": [{"amount": 10}, {"amount": 20}]},
        {},
        ParallelConfig(split_path="items"),
    )

    keys = store.operation_keys
    assert len(keys) == 2, "each branch's tool call must claim its own operation"
    assert len(set(keys)) == 2, f"sibling branches minted the same identity: {keys}"
    assert runner.applications == 2


def test_a_dispatch_outside_a_fan_out_keeps_its_existing_key() -> None:
    """The backward-compatibility pin: only branch dispatches may move.

    Widening the material for a branch orphans that branch's in-flight rows at
    deploy, which is accepted. Moving the key for a *sequential* or token-engine
    dispatch would orphan every other in-flight row for no reason, so the
    no-branch derivation must stay byte-identical to what shipped.
    """
    from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
    from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor
    from zeroth.runtime.runs import Run

    runner = _CountingUnitRunner()
    dispatcher = NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=RuntimeToolExecutor(executable_unit_runner=runner),
    )
    run = Run(run_id="run-seq-1", graph_version_ref="g:v1", deployment_ref="d")
    run.metadata["token_dispatch"] = {
        "dispatch_id": "dsp-1",
        "idempotency_key": "idem-1",
        "attempt": 0,
    }

    identity = dispatcher._operation_identity_for(run, "unit://charge-card")

    assert identity.target_ref == "unit://charge-card"
    assert identity.operation_key == derive_operation_key(
        run_id="run-seq-1",
        idempotency_key="idem-1",
        target_ref="unit://charge-card",
        call_ordinal=0,
    )


# ---------------------------------------------------------------------------
# ZER-49 A16-16 -- the mirror defect: an id-less provider under-merged
# ---------------------------------------------------------------------------


def _ai_message(*calls: dict) -> dict:
    return {"tool_calls": [dict(call) for call in calls]}


def test_an_id_less_tool_call_gets_the_same_id_on_every_extraction() -> None:
    """A16-16: a replayed turn must be recognisable as the same call.

    ``CallableProviderAdapter.ainvoke`` normalises through ``extract_tool_calls``,
    whose id is what ``RuntimeToolExecutor`` keys the operation on. Minting a
    random id there made every extraction of the SAME call a new logical
    operation, so the durable dedupe could never fire -- the exact inverse of the
    fan-out defect, and just as silent.
    """
    from zeroth.runtime.agents.tooling.tool_calls import extract_tool_calls

    (first,) = extract_tool_calls(_ai_message({"name": "charge", "args": {"amount": 10}}))
    (replay,) = extract_tool_calls(_ai_message({"name": "charge", "args": {"amount": 10}}))

    assert first["id"], "the id must stay truthy or the executor falls back to a counter"
    assert first["id"] == replay["id"]


def test_id_less_tool_calls_within_one_turn_stay_distinct() -> None:
    """The control: stability must not collapse two calls the model really made.

    Two identical calls in one assistant message are two effects requested, not
    one requested twice, so their ordinal keeps them apart. A content-only id
    would merge them and drop real work.
    """
    from zeroth.runtime.agents.tooling.tool_calls import extract_tool_calls

    same_args = extract_tool_calls(
        _ai_message(
            {"name": "charge", "args": {"amount": 10}},
            {"name": "charge", "args": {"amount": 10}},
        )
    )
    different_args = extract_tool_calls(
        _ai_message(
            {"name": "charge", "args": {"amount": 10}},
            {"name": "charge", "args": {"amount": 20}},
        )
    )

    assert len({call["id"] for call in same_args}) == 2
    assert len({call["id"] for call in different_args}) == 2


def test_an_id_less_provider_replay_derives_the_same_operation_key() -> None:
    """Close the loop through the executor that actually mints the identity.

    Asserting id stability alone would still allow the key to move; this drives
    the same normalized call through ``build()`` twice, the way a recovered
    worker replays a turn, and pins that the operation key is reproduced.
    """
    import asyncio

    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData
    from zeroth.runtime.agents.tooling.tool_calls import extract_tool_calls

    node = ExecutableUnitNode(
        node_id="tool-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://send-email",
            execution_mode="wrapped_command",
        ),
    )
    graph = _GraphOf(node)

    def _factory(target_ref: str, ordinal: int):
        return operation_identity(
            run_id="run_1",
            dispatch_id="dsp_abc",
            idempotency_key="idem_abc",
            attempt=0,
            target_ref=target_ref,
            call_ordinal=ordinal,
        )

    class _Binding:
        alias = "send_email"
        executable_unit_ref = "node://tool-1"

    def _key_for_a_fresh_extraction():
        (call,) = extract_tool_calls(_ai_message({"name": "send", "args": {"to": "a@b.c"}}))
        runner = _RecordingRunner()
        execute = _executor(runner).build(graph, {}, operation_identity_factory=_factory)
        asyncio.run(execute(_Binding(), call["args"], call["id"]))
        return runner.calls[0]["operation_identity"].operation_key

    assert _key_for_a_fresh_extraction() == _key_for_a_fresh_extraction()


# ---------------------------------------------------------------------------
# ZER-49 F-01 -- a content name is not an occurrence name
# ---------------------------------------------------------------------------


def _send_email_node(manifest_ref: str = "unit://send-email"):
    from zeroth.contracts.graph import ExecutableUnitNode, ExecutableUnitNodeData

    return ExecutableUnitNode(
        node_id="tool-1",
        graph_version_ref="g:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref=manifest_ref,
            execution_mode="wrapped_command",
        ),
    )


def _tool_binding():
    class _Binding:
        alias = "send_email"
        executable_unit_ref = "node://tool-1"

    return _Binding()


def _one_dispatch_factory(target_ref: str, ordinal: int):
    """The factory a dispatch installs: everything but the ordinal is fixed.

    ``idempotency_key`` is staged once per node dispatch and does not move
    between the agent's turns, which is why it cannot discriminate them.
    """
    return operation_identity(
        run_id="run_1",
        dispatch_id="dsp_abc",
        idempotency_key="idem_abc",
        attempt=0,
        target_ref=target_ref,
        call_ordinal=ordinal,
    )


def _keys_for_one_dispatch(turns: list[tuple[dict, ...]]) -> list[str]:
    """Drive ONE node dispatch through several provider turns.

    The executor is built once, the way ``_dispatch_agent`` builds it, and each
    turn is normalised through ``extract_tool_calls`` the way an id-less
    provider adapter normalises it.
    """
    import asyncio

    from zeroth.runtime.agents.tooling.tool_calls import extract_tool_calls

    runner = _RecordingRunner()
    execute = _executor(runner).build(
        _GraphOf(_send_email_node()), {}, operation_identity_factory=_one_dispatch_factory
    )

    async def _drive() -> None:
        for turn in turns:
            for call in extract_tool_calls(_ai_message(*turn)):
                await execute(_tool_binding(), call["args"], call["id"])

    asyncio.run(_drive())
    return [call["operation_identity"].operation_key for call in runner.calls]


def test_two_provider_turns_in_one_dispatch_are_distinct_operations() -> None:
    """F-01: the agent's second turn is a second effect, not a replay of the first.

    The synthetic id is derived from the call's content, so a second turn
    requesting the same tool with the same arguments re-derives the first
    turn's id -- and nothing else in the material moves, because the ordinal
    restarts inside every ``extract_tool_calls`` and the dispatch's
    ``idempotency_key`` is staged once for the whole tool loop. The executor
    then read the second, real request as a repeat: the guard returned the
    stored output and the call never executed, so the model was fed the first
    call's result as though its new request had run.
    """
    turn = ({"name": "send", "args": {"to": "a@b.c"}},)

    keys = _keys_for_one_dispatch([turn, turn])

    assert len(keys) == 2
    assert keys[0] != keys[1], "the second turn was suppressed as a replay of the first"


def test_a_replayed_dispatch_reproduces_each_turn_its_own_identity() -> None:
    """The other half: distinctness must not be bought with fresh randomness.

    A uuid4 per extraction also makes turns distinct -- and destroys the reason
    the key exists, because a recovering worker then re-derives new keys, claims
    them as first executions and re-applies every effect. Recovery re-runs the
    node dispatch from its first turn, so replaying the same turn sequence must
    reproduce the same key sequence, position by position.
    """
    first = ({"name": "send", "args": {"to": "a@b.c"}},)
    second = ({"name": "send", "args": {"to": "a@b.c"}},)

    before_crash = _keys_for_one_dispatch([first, second])
    after_recovery = _keys_for_one_dispatch([first, second])

    assert after_recovery == before_crash
    assert len(set(before_crash)) == 2


def test_a_provider_issued_id_still_bypasses_the_dispatch_counter() -> None:
    """The bound: only the ids this runtime mints lose the id-keyed treatment.

    A real Anthropic/OpenAI call id names an occurrence, so it must keep keying
    the operation on its own -- that is what lets the same call replay at a
    different position and still be recognised. Distinguishing the two cases by
    the synthetic prefix is what keeps this path untouched.
    """
    import asyncio

    runner = _RecordingRunner()
    execute = _executor(runner).build(
        _GraphOf(_send_email_node()), {}, operation_identity_factory=_one_dispatch_factory
    )

    async def _drive() -> None:
        await execute(_tool_binding(), {"to": "a@b.c"}, "toolu_01xyz")
        await execute(_tool_binding(), {"to": "a@b.c"}, "toolu_01xyz")

    asyncio.run(_drive())

    keys = [call["operation_identity"].operation_key for call in runner.calls]
    assert keys[0] == keys[1], "a repeated provider call id is a retry and must still merge"


# ---------------------------------------------------------------------------
# ZER-49 F-14.1 -- the key material must be a function of the call
# ---------------------------------------------------------------------------


_PROCESS_STABILITY_PROBE = """
import asyncio

from zeroth.contracts.graph import (
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    operation_identity,
)
from zeroth.runtime.agents.tooling.tool_calls import _synthetic_call_id
from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor

ARGS = {"labels": {"a", "b", "c", "d", "e", "f", "g", "h"}}


class _Result:
    output_data = {"ok": True}
    audit_record = {}


class _Runner:
    def __init__(self):
        self.identities = []

    async def run(self, manifest_ref, input_payload, *, enforcement_context=None,
                  operation_identity=None):
        self.identities.append(operation_identity)
        return _Result()


class _GraphOf:
    def __init__(self, *nodes):
        self.nodes = list(nodes)
        self.edges = []


class _Binding:
    alias = "tag"
    executable_unit_ref = "node://tool-1"


node = ExecutableUnitNode(
    node_id="tool-1",
    graph_version_ref="g:v1",
    input_contract_ref="contract://input",
    output_contract_ref="contract://output",
    executable_unit=ExecutableUnitNodeData(
        manifest_ref="unit://tag", execution_mode="wrapped_command",
    ),
)


def factory(target_ref, ordinal):
    return operation_identity(
        run_id="run_1", dispatch_id="d", idempotency_key="idem",
        attempt=0, target_ref=target_ref, call_ordinal=ordinal,
    )


runner = _Runner()
execute = RuntimeToolExecutor(executable_unit_runner=runner).build(
    _GraphOf(node), {}, operation_identity_factory=factory
)
asyncio.run(execute(_Binding(), ARGS, "toolu_provider_1"))
print(_synthetic_call_id(0, "tag", ARGS))
print(runner.identities[0].operation_key)
"""


def _under_hash_seed(seed: str) -> tuple[str, str]:
    """Derive the two digests in a fresh interpreter with a given hash seed."""
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", _PROCESS_STABILITY_PROBE],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    assert result.returncode == 0, result.stderr
    call_id, operation_key = result.stdout.split()
    return call_id, operation_key


def test_unordered_arguments_digest_the_same_in_every_process() -> None:
    """F-14.1: a digest that moves between processes is not an identity.

    The material was built with ``default=str``, so a ``set`` argument
    contributed the iteration order of that particular interpreter -- which
    ``PYTHONHASHSEED`` randomises by default. A recovered worker therefore
    derived a different operation key for the same call and claimed it as a
    first execution, re-applying the effect. Reachable through the callable
    adapter, which is the same adapter that needs the determinism.
    """
    derived = {_under_hash_seed(seed) for seed in ("1", "2", "3", "4")}

    assert len(derived) == 1, f"the digests moved with the hash seed: {derived}"


def test_mixed_type_argument_keys_do_not_abort_the_turn() -> None:
    """F-14.1: ``sort_keys=True`` raised ``TypeError`` on ``{1: .., "b": ..}``.

    Naming a call is not allowed to fail -- an exception here propagates out of
    normalisation and ends the agent turn, so a payload shape decided one layer
    above the runtime could kill a run. The digest must also still discriminate,
    or totality would have been bought by returning a constant.
    """
    from zeroth.runtime.agents.tooling.tool_calls import _synthetic_call_id

    first = _synthetic_call_id(0, "tag", {"rows": {1: "a", "b": 2}})
    other = _synthetic_call_id(0, "tag", {"rows": {1: "a", "b": 3}})

    assert first.startswith("zcall_")
    assert first != other


def test_the_executor_digests_unserializable_arguments_without_raising() -> None:
    """The same material is hashed again in the executor, so it fails the same way.

    ``args_digest`` is key material for every provider-issued call id, so a
    ``TypeError`` here aborts the tool call rather than merely mis-naming it.
    """
    import asyncio

    runner = _RecordingRunner()
    execute = _executor(runner).build(
        _GraphOf(_send_email_node()), {}, operation_identity_factory=_one_dispatch_factory
    )

    asyncio.run(execute(_tool_binding(), {"rows": {1: "a", "b": 2}}, "toolu_01xyz"))

    assert runner.calls[0]["operation_identity"].operation_key


def test_an_ordinary_payload_keeps_the_material_it_already_had() -> None:
    """The migration bound: JSON-safe arguments must not be re-keyed.

    Every real payload takes the plain-serialization branch, so hardening the
    fallback cannot orphan an in-flight operation record.
    """
    import json

    from zeroth.runtime.agents.tooling.tool_calls import canonical_json

    payload = {"to": "a@b.c", "cc": ["x", "y"], "n": 3, "ok": True, "z": None}

    assert canonical_json(payload) == json.dumps(payload, sort_keys=True)


# ---------------------------------------------------------------------------
# ZER-49 F-14.2 -- concatenated key material must not be re-cuttable
# ---------------------------------------------------------------------------


def _key_through_the_dispatcher(
    manifest_ref: str,
    *,
    branch_id: str | None = None,
    call_id: str | None = None,
) -> str:
    """Derive an agent tool call's key through the real dispatcher composition.

    ``_operation_identity_for`` is where ``#branch:<id>`` is appended, so the
    alias only shows up when both halves of the concatenation are measured
    together.
    """
    import asyncio

    from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
    from zeroth.runtime.runs import Run

    runner = _RecordingRunner()
    dispatcher = NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=_executor(runner),
    )
    run = Run(run_id="run_1", graph_version_ref="g:v1", deployment_ref="d")
    execute = _executor(runner).build(
        _GraphOf(_send_email_node(manifest_ref)),
        {},
        operation_identity_factory=lambda target_ref, ordinal: dispatcher._operation_identity_for(
            run, target_ref, call_ordinal=ordinal, branch_id=branch_id
        ),
    )
    asyncio.run(execute(_tool_binding(), {}, call_id))
    return runner.calls[0]["operation_identity"].operation_key


def test_a_manifest_ref_cannot_forge_a_branch_discriminator() -> None:
    """F-14.2: the branch suffix was appended to unescaped material.

    A ref that already reads ``...#branch:<id>`` produced, outside any fan-out,
    the exact string a plain ref produces inside that branch -- so two unrelated
    operations shared one durable record, and one of them would be suppressed
    holding the other's receipt. It takes an adversarial ``manifest_ref``, but
    the material gates side effects, so the absence of escaping is the defect.
    """
    smuggled = _key_through_the_dispatcher("unit://x#branch:run_1:branch:0")
    genuine = _key_through_the_dispatcher("unit://x", branch_id="run_1:branch:0")

    assert smuggled != genuine


def test_a_manifest_ref_cannot_re_cut_the_call_id_boundary() -> None:
    """The same absence, one separator earlier.

    ``ref#call_id#digest`` does not say where the ref ends, so a ref carrying a
    ``#`` and a call id carrying one describe the same joined string from two
    different (ref, id) pairs.
    """
    left = _key_through_the_dispatcher("unit://x#toolu_a", call_id="toolu_b")
    right = _key_through_the_dispatcher("unit://x", call_id="toolu_a#toolu_b")

    assert left != right


def test_an_ordinary_ref_is_not_re_keyed_by_the_escaping() -> None:
    """Escaping is only allowed to move the keys it has to move.

    A ref containing neither ``#`` nor ``%`` -- every ref in practice -- must
    hash exactly as it did before, or the fix would orphan every in-flight
    operation record at deploy for a defect that needs an adversarial ref.
    """
    key = _key_through_the_dispatcher("unit://send-email")

    assert key == derive_operation_key(
        run_id="run_1",
        idempotency_key="run_1",
        target_ref="unit://send-email",
        call_ordinal=0,
    )
