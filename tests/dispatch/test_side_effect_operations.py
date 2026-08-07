"""Durable side-effect operation records: state, convergence, reconciliation.

ZER-26 R3/R4/R5/R10. These run on both SQLite and Postgres through the
``dual_database`` fixture, which also runs migrations -- so a passing run is
evidence that revision 020 upgrades cleanly on both backends.
"""

from __future__ import annotations

import pytest

from tests.conftest import requires_docker
from zeroth.platform.dispatch.operations import (
    OperationState,
    SideEffectOperationStore,
)

RUN = "run-1"
DISPATCH = "dsp-1"
IDEM = "idem-1"
TARGET = "unit://charge-card"
KEY = "op_key_1"


async def _claim(store: SideEffectOperationStore, key: str = KEY, **overrides: object):
    kwargs: dict[str, object] = {
        "run_id": RUN,
        "dispatch_id": DISPATCH,
        "idempotency_key": IDEM,
        "target_ref": TARGET,
        "attempt": 0,
        "support": "at_least_once",
    }
    kwargs.update(overrides)
    return await store.claim(key, **kwargs)  # type: ignore[arg-type]


@requires_docker
class TestSideEffectOperationsDualBackend:
    """Every guarantee below must hold identically on SQLite and Postgres."""

    # -- R3: the five states are real and durable ---------------------------

    async def test_first_claim_is_in_flight_and_authorises_execution(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)

        claim = await _claim(store)

        assert claim.first_execution is True
        assert claim.state is OperationState.IN_FLIGHT
        record = await store.get(KEY)
        assert record is not None
        assert record["state"] == OperationState.IN_FLIGHT

    async def test_completion_and_failure_are_persisted_distinctly(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.complete(KEY, receipt='{"charge":"ok"}')

        await _claim(store, "op_key_2")
        await store.fail("op_key_2", error="card declined")

        completed = await store.get(KEY)
        failed = await store.get("op_key_2")
        assert completed is not None and failed is not None
        assert completed["state"] == OperationState.COMPLETED
        assert completed["receipt"] == '{"charge":"ok"}'
        assert failed["state"] == OperationState.FAILED
        assert failed["error"] == "card declined"

    async def test_unknown_operation_reads_as_absent(self, dual_database) -> None:
        """NOT_STARTED is the absence of a record, not a stored row.

        Writing a row before the effect is attempted would itself be a side
        effect that recovery could not distinguish from a real attempt.
        """
        store = SideEffectOperationStore(dual_database)

        assert await store.get("op_never_seen") is None
        assert await store.state_of("op_never_seen") is OperationState.NOT_STARTED

    # -- R5: convergence ----------------------------------------------------

    async def test_duplicate_completion_keeps_the_first_result(self, dual_database) -> None:
        """The first COMPLETED result wins.

        Two workers can both believe they own the operation. If the later report
        overwrote the earlier one, the stored outcome would depend on arrival
        order -- exactly the non-determinism the receipt store exists to remove.
        """
        store = SideEffectOperationStore(dual_database)
        await _claim(store)

        first = await store.complete(KEY, receipt='{"charge":"first"}')
        second = await store.complete(KEY, receipt='{"charge":"second"}')

        assert first is True
        assert second is False
        record = await store.get(KEY)
        assert record is not None
        assert record["receipt"] == '{"charge":"first"}'

    async def test_failure_cannot_overwrite_a_completed_operation(self, dual_database) -> None:
        """A late failure report must not erase a known success."""
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.complete(KEY, receipt='{"charge":"ok"}')

        await store.fail(KEY, error="late timeout")

        record = await store.get(KEY)
        assert record is not None
        assert record["state"] == OperationState.COMPLETED
        assert record["receipt"] == '{"charge":"ok"}'

    async def test_replay_of_a_completed_operation_is_suppressed(self, dual_database) -> None:
        """The crash-after-success case: recovery must not re-apply the effect."""
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.complete(KEY, receipt='{"charge":"ok"}')

        replay = await _claim(store, attempt=1)

        assert replay.first_execution is False
        assert replay.state is OperationState.COMPLETED
        assert replay.receipt == '{"charge":"ok"}'

    async def test_a_failed_operation_may_be_retried(self, dual_database) -> None:
        """A confirmed failure is not ambiguous -- re-execution is safe."""
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.fail(KEY, error="connection refused")

        retry = await _claim(store, attempt=1)

        assert retry.first_execution is True
        assert retry.state is OperationState.IN_FLIGHT

    # -- R4: ambiguity and bounded reconciliation ---------------------------

    async def test_claiming_an_in_flight_operation_is_ambiguous_not_failed(
        self, dual_database
    ) -> None:
        """Crash after dispatch, before the completion checkpoint.

        The record is still IN_FLIGHT, so the effect may or may not have
        applied. Reporting that as FAILED would be a claim the runtime cannot
        support; it becomes AMBIGUOUS and demands reconciliation instead.
        """
        store = SideEffectOperationStore(dual_database)
        await _claim(store)

        resumed = await _claim(store, attempt=1)

        assert resumed.state is OperationState.AMBIGUOUS
        assert resumed.first_execution is False
        assert resumed.reconciliation_required is True
        record = await store.get(KEY)
        assert record is not None
        assert record["state"] == OperationState.AMBIGUOUS

    async def test_ambiguity_is_never_reported_as_confirmed_failure(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.mark_ambiguous(KEY, reason="timeout awaiting receipt")

        record = await store.get(KEY)
        assert record is not None
        assert record["state"] == OperationState.AMBIGUOUS
        assert record["state"] != OperationState.FAILED

    async def test_reconciliation_resolves_to_the_discovered_outcome(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.mark_ambiguous(KEY, reason="timeout")

        state = await store.record_reconciliation(
            KEY, resolved=True, receipt='{"charge":"was applied"}'
        )

        assert state is OperationState.COMPLETED
        record = await store.get(KEY)
        assert record is not None
        assert record["receipt"] == '{"charge":"was applied"}'

    async def test_duplicate_reconciliation_converges(self, dual_database) -> None:
        """Two reconcilers racing must leave one result, not the last writer's."""
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.mark_ambiguous(KEY, reason="timeout")

        await store.record_reconciliation(KEY, resolved=True, receipt='{"charge":"first"}')
        await store.record_reconciliation(KEY, resolved=True, receipt='{"charge":"second"}')

        record = await store.get(KEY)
        assert record is not None
        assert record["state"] == OperationState.COMPLETED
        assert record["receipt"] == '{"charge":"first"}'

    async def test_reconciliation_attempts_are_bounded(self, dual_database) -> None:
        """An unresolvable ambiguity must stop asking, without inventing a verdict.

        The budget is what keeps recovery from looping forever on an external
        system that cannot answer. Exhaustion is surfaced, not converted into
        FAILED -- the runtime still does not know what happened.
        """
        store = SideEffectOperationStore(dual_database, max_reconciliation_attempts=2)
        await _claim(store)
        await store.mark_ambiguous(KEY, reason="timeout")

        await store.record_reconciliation(KEY, resolved=False, error="no outcome endpoint")
        await store.record_reconciliation(KEY, resolved=False, error="no outcome endpoint")

        claim = await _claim(store, attempt=2)
        record = await store.get(KEY)
        assert record is not None
        assert record["state"] == OperationState.AMBIGUOUS
        assert record["reconciliation_attempts"] == 2
        assert claim.reconciliation_exhausted is True
        assert claim.first_execution is False

    async def test_pending_reconciliation_is_durable_work(self, dual_database) -> None:
        """Ambiguity survives process death as queryable work, not an in-memory flag."""
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.mark_ambiguous(KEY, reason="timeout")

        reopened = SideEffectOperationStore(dual_database)
        pending = await reopened.pending_reconciliation(RUN)

        assert [row["operation_key"] for row in pending] == [KEY]

    # -- R10: the residual guarantee is visible ------------------------------

    async def test_support_is_recorded_on_the_operation(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)

        await _claim(store, support="at_least_once")
        await _claim(store, "op_key_idem", support="idempotent")

        weak = await store.get(KEY)
        strong = await store.get("op_key_idem")
        assert weak is not None and strong is not None
        assert weak["support"] == "at_least_once"
        assert weak["dedupe_supported"] is False
        assert strong["support"] == "idempotent"
        assert strong["dedupe_supported"] is True

    async def test_at_least_once_ambiguity_reports_residual_duplicate_risk(
        self, dual_database
    ) -> None:
        """The honest case: no dedupe upstream and no outcome query.

        Re-executing may double-apply. The record has to say so, because this is
        the one situation the runtime cannot make safe on the integration's behalf.
        """
        store = SideEffectOperationStore(dual_database)
        await _claim(store, support="at_least_once")

        resumed = await _claim(store, attempt=1)

        assert resumed.state is OperationState.AMBIGUOUS
        assert resumed.residual_duplicate_risk is True

    async def test_idempotent_target_carries_no_residual_risk(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)
        await _claim(store, support="idempotent")

        resumed = await _claim(store, attempt=1, support="idempotent")

        assert resumed.state is OperationState.AMBIGUOUS
        assert resumed.residual_duplicate_risk is False


@pytest.mark.parametrize(
    "state",
    [
        OperationState.NOT_STARTED,
        OperationState.IN_FLIGHT,
        OperationState.COMPLETED,
        OperationState.FAILED,
        OperationState.AMBIGUOUS,
    ],
)
def test_every_required_state_exists(state: OperationState) -> None:
    """R3 names exactly these five; pin them so none is quietly dropped."""
    assert isinstance(state.value, str)


# ---------------------------------------------------------------------------
# End-to-end: the acceptance criterion this whole change exists for
# ---------------------------------------------------------------------------


class _CountingRunner:
    """An external integration that records how often the effect was applied."""

    def __init__(self) -> None:
        self.applications = 0

    async def run(self, manifest_ref, input_payload, *, enforcement_context=None,
                  operation_identity=None):
        self.applications += 1
        return _RunResult({"charged": True, "applications": self.applications})


class _RunResult:
    def __init__(self, output: dict) -> None:
        self.output_data = output
        self.audit_record: dict = {}


def _charge_node():
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


def _dispatcher(store, runner):
    from zeroth.runtime.orchestration.dispatcher import NodeDispatcher
    from zeroth.runtime.orchestration.tool_executor import RuntimeToolExecutor

    return NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=RuntimeToolExecutor(executable_unit_runner=runner),
        operation_store=store,
    )


async def _dispatch_once(dispatcher, run):
    return await dispatcher._dispatch_executable_unit(_charge_node(), run, {"amount": 10})


def _run_with_dispatch(dispatch_id: str = "dsp-1"):
    from zeroth.runtime.runs import Run

    run = Run(run_id="run-charge-1", graph_version_ref="g:v1", deployment_ref="d")
    run.metadata["token_dispatch"] = {
        "dispatch_id": dispatch_id,
        "idempotency_key": "idem-charge-1",
        "attempt": 0,
    }
    return run


@requires_docker
class TestSideEffectSuppressionEndToEnd:
    async def test_crash_after_success_does_not_apply_the_effect_twice(
        self, dual_database
    ) -> None:
        """The acceptance criterion, exercised through the real dispatch path.

        The first dispatch succeeds and its receipt is stored. The second stands
        in for recovery after a crash that lost the completion checkpoint: it
        must return the stored result rather than charging the card again.
        """
        store = SideEffectOperationStore(dual_database)
        runner = _CountingRunner()
        dispatcher = _dispatcher(store, runner)

        first_output, _ = await _dispatch_once(dispatcher, _run_with_dispatch())
        second_output, second_audit = await _dispatch_once(dispatcher, _run_with_dispatch())

        assert runner.applications == 1
        assert first_output == {"charged": True, "applications": 1}
        assert second_output == first_output
        assert second_audit["side_effect_operation"]["replay_suppressed"] is True

    async def test_a_different_logical_operation_is_not_suppressed(self, dual_database) -> None:
        """The positive control: suppression must not swallow genuine new work."""
        store = SideEffectOperationStore(dual_database)
        runner = _CountingRunner()
        dispatcher = _dispatcher(store, runner)

        await _dispatch_once(dispatcher, _run_with_dispatch())
        other = _run_with_dispatch()
        other.metadata["token_dispatch"]["idempotency_key"] = "idem-charge-2"
        await _dispatch_once(dispatcher, other)

        assert runner.applications == 2

    async def test_without_a_store_behaviour_is_unchanged(self, dual_database) -> None:
        """R9: deployments that have not opted in keep executing exactly as before."""
        runner = _CountingRunner()
        dispatcher = _dispatcher(None, runner)

        await _dispatch_once(dispatcher, _run_with_dispatch())
        await _dispatch_once(dispatcher, _run_with_dispatch())

        assert runner.applications == 2

    async def test_a_timeout_is_recorded_as_ambiguous_not_failed(self, dual_database) -> None:
        """A timeout is the one outcome where the effect may still have landed."""

        class _TimingOutRunner:
            async def run(self, manifest_ref, input_payload, *, enforcement_context=None,
                          operation_identity=None):
                raise TimeoutError("no receipt within deadline")

        store = SideEffectOperationStore(dual_database)
        dispatcher = _dispatcher(store, _TimingOutRunner())
        run = _run_with_dispatch()

        with pytest.raises(TimeoutError):
            await _dispatch_once(dispatcher, run)

        pending = await store.pending_reconciliation(run.run_id)
        assert len(pending) == 1
        assert pending[0]["state"] == OperationState.AMBIGUOUS

    async def test_a_confirmed_failure_is_recorded_as_failed(self, dual_database) -> None:
        """A refusal the integration actually reported is not ambiguous."""

        class _FailingRunner:
            async def run(self, manifest_ref, input_payload, *, enforcement_context=None,
                          operation_identity=None):
                raise RuntimeError("card declined")

        store = SideEffectOperationStore(dual_database)
        dispatcher = _dispatcher(store, _FailingRunner())
        run = _run_with_dispatch()

        with pytest.raises(RuntimeError):
            await _dispatch_once(dispatcher, run)

        assert await store.pending_reconciliation(run.run_id) == []

    async def test_outcomes_are_counted_as_distinct_metrics(self, dual_database) -> None:
        """R8: first execution and replay suppression are separately countable."""

        class _Collector:
            def __init__(self) -> None:
                self.counts: dict[str, int] = {}

            def increment(self, name: str, *args, **kwargs) -> None:
                self.counts[name] = self.counts.get(name, 0) + 1

            def observe(self, *args, **kwargs) -> None:
                pass

        collector = _Collector()
        store = SideEffectOperationStore(dual_database, metrics_collector=collector)
        dispatcher = _dispatcher(store, _CountingRunner())

        await _dispatch_once(dispatcher, _run_with_dispatch())
        await _dispatch_once(dispatcher, _run_with_dispatch())

        assert collector.counts.get("zeroth_side_effect_first_execution_total") == 1
        assert collector.counts.get("zeroth_side_effect_replay_suppressed_total") == 1
