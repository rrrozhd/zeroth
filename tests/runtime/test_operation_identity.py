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
