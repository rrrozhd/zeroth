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

    async def test_a_repeat_completion_with_the_same_receipt_reports_it_stored_nothing(
        self, dual_database
    ) -> None:
        """The idempotent re-report must still answer "I did not store this".

        The earlier proof used two *different* receipts, so a bug that compared
        the stored receipt to the supplied one passed it. Re-reporting the same
        result is exactly the case that comparison got wrong.
        """
        store = SideEffectOperationStore(dual_database)
        await _claim(store)

        first = await store.complete(KEY, receipt='{"charge":"ok"}')
        again = await store.complete(KEY, receipt='{"charge":"ok"}')

        assert first is True
        assert again is False

    async def test_completing_an_unknown_operation_reports_false(self, dual_database) -> None:
        store = SideEffectOperationStore(dual_database)

        assert await store.complete("op_never_claimed", receipt="{}") is False

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


def test_the_state_vocabulary_is_exactly_the_five_required() -> None:
    """R3 names exactly these five -- no more, no fewer.

    The previous version of this test asserted ``isinstance(state.value, str)``
    for each member, which the initial audit correctly called vacuous: it passes
    for any enum whatsoever and would not notice a dropped or renamed state.
    """
    assert {state.value for state in OperationState} == {
        "NOT_STARTED",
        "IN_FLIGHT",
        "COMPLETED",
        "FAILED",
        "AMBIGUOUS",
    }


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
        assert second_audit["operation_replay_suppressed"] is True

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


# ---------------------------------------------------------------------------
# ZER26-AUD-001: the wiring itself, through the real service bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_service_bootstrap_wires_a_real_operation_store(sqlite_db) -> None:
    """The store must reach live executions, not just hand-built dispatchers.

    The initial audit caught that every end-to-end proof here constructed its own
    NodeDispatcher, which exercises the mechanism while leaving the wiring
    unverified -- `operation_store` defaulted to None everywhere in production.
    This asserts the bootstrap-built orchestrator actually carries a store and
    that the dispatcher the driver uses is the same one.
    """
    from tests.service.helpers import agent_graph, deploy_service
    from zeroth.platform.dispatch.operations import SideEffectOperationStore as _Store

    graph = agent_graph(graph_id="boot-side-effects")
    svc, _dep = await deploy_service(sqlite_db, graph, deployment_ref="boot-side-effects")

    orchestrator = svc.orchestrator
    assert isinstance(orchestrator.operation_store, _Store), (
        "bootstrap_service must construct a SideEffectOperationStore"
    )
    assert orchestrator._node_dispatcher.operation_store is orchestrator.operation_store


# ---------------------------------------------------------------------------
# ZER26-AUD-002: reconciliation is consulted first, and the budget really stops
# ---------------------------------------------------------------------------


async def _seed_ambiguous(store, identity) -> None:
    """Leave an operation in the state a vanished attempt would leave it."""
    await store.claim(
        identity.operation_key,
        run_id=identity.run_id,
        dispatch_id=identity.dispatch_id,
        idempotency_key=identity.idempotency_key,
        target_ref=identity.target_ref,
        attempt=identity.attempt,
        support=identity.support.value,
    )
    await store.mark_ambiguous(identity.operation_key, reason="crash before checkpoint")


@requires_docker
class TestAmbiguousReconciliationOnTheDispatchPath:
    async def test_the_outcome_lookup_runs_before_any_re_execution(self, dual_database) -> None:
        """Blind re-execution is the thing this subsystem exists to avoid.

        A resolvable ambiguity must be answered by asking the integration what
        happened, not by doing it again and hoping the target dedupes.
        """
        store = SideEffectOperationStore(dual_database)
        runner = _CountingRunner()
        asked: list[str] = []

        async def _lookup(identity):
            asked.append(identity.operation_key)
            return '{"charged": true, "applications": 99}'

        dispatcher = _dispatcher(store, runner)
        object.__setattr__(dispatcher, "operation_outcome_lookup", _lookup)

        run = _run_with_dispatch()
        identity = dispatcher._operation_identity_for(run, "unit://charge-card")
        # Seed an attempt that vanished mid-flight: claim without completing,
        # then mark it ambiguous. Running a *successful* dispatch first would
        # leave it COMPLETED, and mark_ambiguous deliberately refuses to move a
        # completed row.
        await _seed_ambiguous(store, identity)
        before = runner.applications

        output, audit = await _dispatch_once(dispatcher, run)

        assert asked == [identity.operation_key], "the outcome lookup must be consulted"
        assert runner.applications == before, "the effect must not be re-applied"
        assert output == {"charged": True, "applications": 99}
        assert audit["operation_replay_suppressed"] is True

    async def test_an_exhausted_budget_refuses_rather_than_re_executing(
        self, dual_database
    ) -> None:
        """The budget is a real stop, not a counter that gets logged.

        Once it is spent the runtime still does not know whether the effect
        landed, so re-executing could double-apply. Refusing and leaving durable
        work is the only honest option.
        """
        from zeroth.runtime.orchestration.dispatcher import (
            SideEffectReconciliationExhaustedError,
        )

        store = SideEffectOperationStore(dual_database, max_reconciliation_attempts=1)
        runner = _CountingRunner()
        dispatcher = _dispatcher(store, runner)
        run = _run_with_dispatch()
        identity = dispatcher._operation_identity_for(run, "unit://charge-card")
        await _seed_ambiguous(store, identity)
        await store.record_reconciliation(identity.operation_key, resolved=False, error="no lookup")
        before = runner.applications

        with pytest.raises(SideEffectReconciliationExhaustedError):
            await _dispatch_once(dispatcher, run)

        assert runner.applications == before, "an exhausted operation must not re-execute"
        assert (await store.get(identity.operation_key))["state"] == OperationState.AMBIGUOUS

    async def test_an_unresolvable_ambiguity_within_budget_may_re_execute(
        self, dual_database
    ) -> None:
        """At-least-once is still the contract when nothing can be asked.

        With no lookup available and budget remaining, re-execution is allowed --
        but only after the attempt is recorded, so the residual duplicate risk
        is visible rather than silent.
        """
        store = SideEffectOperationStore(dual_database, max_reconciliation_attempts=5)
        runner = _CountingRunner()
        dispatcher = _dispatcher(store, runner)
        run = _run_with_dispatch()

        identity = dispatcher._operation_identity_for(run, "unit://charge-card")
        await _seed_ambiguous(store, identity)
        before = runner.applications

        await _dispatch_once(dispatcher, run)

        assert runner.applications == before + 1
        record = await store.get(identity.operation_key)
        assert record["reconciliation_attempts"] >= 1, "the attempt must be recorded"
        # The retry must run through the same checkpoint a first execution uses.
        # Without this the test passed against the bypass bug it exists for: the
        # effect was re-applied and the operation stayed AMBIGUOUS forever.
        assert record["state"] == OperationState.COMPLETED, (
            "a successful retry must settle the operation, not leave it ambiguous"
        )
        assert record["receipt"] is not None


# ---------------------------------------------------------------------------
# ZER26-AUD-003: the transitions are compare-and-set under real concurrency
# ---------------------------------------------------------------------------


@requires_docker
class TestOperationTransitionsAreCompareAndSet:
    async def test_concurrent_fresh_claims_authorise_exactly_one(self, dual_database) -> None:
        """Two workers claiming the same unseen operation: only one may execute.

        Sequential tests cannot see this; the previous claim() did SELECT then
        INSERT, so both callers could observe "no row" and both proceed.
        """
        import asyncio

        store = SideEffectOperationStore(dual_database)
        claims = await asyncio.gather(*(_claim(store) for _ in range(3)))

        assert sum(1 for c in claims if c.first_execution) == 1

    async def test_concurrent_completions_store_exactly_one_result(self, dual_database) -> None:
        """Only one completer may report that it stored the result."""
        import asyncio

        store = SideEffectOperationStore(dual_database)
        await _claim(store)

        results = await asyncio.gather(
            *(store.complete(KEY, receipt=f'{{"n":{n}}}') for n in range(3))
        )

        assert sum(1 for r in results if r) == 1
        record = await store.get(KEY)
        assert record["state"] == OperationState.COMPLETED

    async def test_concurrent_reconcilers_converge_on_one_receipt(self, dual_database) -> None:
        """The genuinely racing version of the convergence proof.

        The earlier test called the reconcilers sequentially while its docstring
        claimed they raced -- exactly the gap the initial audit flagged.
        """
        import asyncio

        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.mark_ambiguous(KEY, reason="timeout")

        await asyncio.gather(
            *(
                store.record_reconciliation(KEY, resolved=True, receipt=f'{{"n":{n}}}')
                for n in range(3)
            )
        )

        record = await store.get(KEY)
        assert record["state"] == OperationState.COMPLETED
        assert record["receipt"] in {f'{{"n":{n}}}' for n in range(3)}
        stored = record["receipt"]
        assert (await store.get(KEY))["receipt"] == stored, "the stored result must be stable"

    async def test_concurrent_retries_of_a_failed_operation_authorise_one(
        self, dual_database
    ) -> None:
        import asyncio

        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.fail(KEY, error="declined")

        claims = await asyncio.gather(*(_claim(store, attempt=1) for _ in range(3)))

        assert sum(1 for c in claims if c.first_execution) == 1


# ---------------------------------------------------------------------------
# ZER26-AUD-008: the operation audit fields must survive the capture boundary
# ---------------------------------------------------------------------------


def test_operation_audit_keys_survive_the_metadata_only_capture() -> None:
    """Emitting audit fields is not the same as persisting them.

    This drives the *real* AuditCapturePolicy rather than inspecting the
    registry. The earlier version only asserted that the keys were declared, so
    it would have passed against a nested emitter or a broken projection —
    exactly the bug it was written to catch, where the whole
    `side_effect_operation` block was discarded before storage.
    """
    from zeroth.contracts.graph import operation_identity
    from zeroth.governance.audit.capture_policy import AuditCapturePolicy
    from zeroth.governance.audit.models import NodeAuditRecord
    from zeroth.platform.dispatch.operations import OperationClaim
    from zeroth.runtime.orchestration.dispatcher import _operation_audit_fields

    identity = operation_identity(
        run_id="run_1",
        dispatch_id="dsp_abc",
        idempotency_key="idem_abc",
        attempt=0,
        target_ref="unit://charge-card",
    )
    emitted = _operation_audit_fields(
        identity,
        OperationClaim(
            state=OperationState.AMBIGUOUS,
            first_execution=False,
            reconciliation_required=True,
            residual_duplicate_risk=True,
        ),
    )
    record = NodeAuditRecord(
        audit_id="audit-op",
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id="tenant-a",
        status="completed",
        execution_metadata=dict(emitted),
    )

    captured = AuditCapturePolicy().apply(record).execution_metadata

    # The correlating key must come back verbatim: a digest would satisfy a
    # presence check while breaking the ability to join two records for one
    # logical operation.
    assert captured["operation_key"] == identity.operation_key
    assert captured["operation_support"] == "at_least_once"
    assert captured["operation_state"] == "ambiguous"
    assert captured["operation_residual_duplicate_risk"] is True
    assert captured["operation_first_execution"] is False


def test_a_nested_operation_block_would_not_survive_capture() -> None:
    """The negative control for the bug that motivated flattening.

    Without this, "the keys survive" proves nothing about *why* — the flat shape
    is load-bearing, and a future refactor back to a nested block would silently
    stop persisting the fields again.
    """
    from zeroth.governance.audit.capture_policy import AuditCapturePolicy
    from zeroth.governance.audit.models import NodeAuditRecord

    record = NodeAuditRecord(
        audit_id="audit-nested",
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id="tenant-a",
        status="completed",
        execution_metadata={"side_effect_operation": {"operation_key": "op_abc"}},
    )

    captured = AuditCapturePolicy().apply(record).execution_metadata

    assert captured.get("side_effect_operation") != {"operation_key": "op_abc"}


def test_every_operation_state_and_support_value_is_in_its_vocabulary() -> None:
    """A vocabulary that misses a real term silently summarizes it away."""
    from zeroth.contracts.graph import SideEffectSupport
    from zeroth.governance.audit.capture_vocabulary import METADATA_VOCABULARIES

    assert {s.value.lower() for s in OperationState} <= METADATA_VOCABULARIES["operation_state"]
    assert {s.value for s in SideEffectSupport} <= METADATA_VOCABULARIES["operation_support"]


@requires_docker
class TestClaimVersusSettleRaces:
    """A claim racing a settle must report the settled truth, never invent it."""

    async def test_a_claim_racing_a_completion_reports_the_stored_receipt(
        self, dual_database
    ) -> None:
        """The guarded transition must not demote a COMPLETED operation."""
        import asyncio

        store = SideEffectOperationStore(dual_database)
        await _claim(store)  # leaves it IN_FLIGHT

        claim, _ = await asyncio.gather(
            _claim(store, attempt=1),
            store.complete(KEY, receipt='{"charge":"ok"}'),
        )

        record = await store.get(KEY)
        assert record["state"] == OperationState.COMPLETED, "a receipt must never be lost"
        assert record["receipt"] == '{"charge":"ok"}'
        # Whichever order the two landed in, the claim must not authorise work.
        assert claim.first_execution is False

    async def test_a_claim_racing_a_failure_reports_failed_not_ambiguous(
        self, dual_database
    ) -> None:
        """A confirmed failure must not be dressed up as uncertainty.

        Reporting FAILED as AMBIGUOUS would invent doubt and send the caller
        down a reconciliation path with nothing to resolve.
        """
        store = SideEffectOperationStore(dual_database)
        await _claim(store)
        await store.fail(KEY, error="card declined")

        # Re-enter the in-flight branch by putting it back in flight, then let a
        # failure settle it before the claim's guarded update lands.
        await _claim(store, attempt=1)  # FAILED -> IN_FLIGHT
        await store.fail(KEY, error="declined again")
        claim = await _claim(store, attempt=2)

        assert claim.state is OperationState.IN_FLIGHT
        assert claim.first_execution is True, "a confirmed failure is safe to retry"


@requires_docker
class TestExhaustionPausesRatherThanFails:
    """An exhausted ambiguous operation must not be reported as a failure."""

    async def test_pausing_persists_a_resumable_waiting_interrupt_run(
        self, dual_database
    ) -> None:
        """FAILED asserts the effect did not happen. Nobody knows that.

        AUD-002 flagged that exhaustion raised into the driver's generic catch,
        which terminally failed the run — turning "we cannot tell" into "it did
        not happen" and stranding the reconciliation work with no way to resume.

        This drives the real persistence path rather than reading the source: a
        structural check would pass against a method that wrote nothing.
        """
        from zeroth.integrations.persistence.runs import RunRepository
        from zeroth.runtime.orchestration.driver import GraphDriver
        from zeroth.runtime.runs import Run, RunStatus

        repo = RunRepository(dual_database)
        run = await repo.create(Run(graph_version_ref="g:v1", deployment_ref="dep-pause"))

        class _Recorder:
            def redact(self, message: str) -> str:
                return message

        class _Driver:
            run_repository = repo
            audit_recorder = _Recorder()
            emitted: list[str] = []

            async def emit_webhook(self, event_type, run, data):
                type(self).emitted.append(event_type)

        persisted = await GraphDriver.pause_for_reconciliation(
            _Driver(), run, "charge-node", "budget spent"
        )

        assert persisted.status is RunStatus.WAITING_INTERRUPT, (
            "an ambiguous, out-of-retries operation must pause, not fail"
        )
        # run_store rejects WAITING_INTERRUPT without an interrupt id, so a pause
        # that omitted it would be unloadable.
        assert persisted.pending_interrupt_id == "reconcile:charge-node"
        assert persisted.metadata["pending_reconciliation"]["node_id"] == "charge-node"
        assert persisted.failure_state is None, "a pause must not record a failure"

        reloaded = await repo.get(run.run_id)
        assert reloaded.status is RunStatus.WAITING_INTERRUPT, "the pause must be durable"
        assert _Driver.emitted == ["run.waiting_interrupt"]

    async def test_the_dispatch_handler_routes_exhaustion_to_the_pause(
        self, dual_database
    ) -> None:
        """The terminal-vs-resumable decision, exercised rather than read.

        Both outcomes go through one handler, so this drives it twice: an
        exhausted ambiguity must pause, and an ordinary error must still fail.
        Testing only the first would pass against a handler that paused for
        everything.
        """
        from zeroth.integrations.persistence.runs import RunRepository
        from zeroth.runtime.orchestration.dispatcher import (
            SideEffectReconciliationExhaustedError,
        )
        from zeroth.runtime.orchestration.driver import GraphDriver
        from zeroth.runtime.runs import Run, RunStatus

        repo = RunRepository(dual_database)

        class _Recorder:
            def redact(self, message: str) -> str:
                return message

            async def record_failed_execution(self, *args, **kwargs) -> None:
                return None

        class _Driver:
            run_repository = repo
            audit_recorder = _Recorder()

            async def emit_webhook(self, event_type, run, data):
                return None

            fail_run = GraphDriver.fail_run
            pause_for_reconciliation = GraphDriver.pause_for_reconciliation
            refresh_artifact_ttls = staticmethod(lambda run: None)

        async def _settle(exc):
            run = await repo.create(
                Run(graph_version_ref="g:v1", deployment_ref="dep-settle")
            )
            driver = _Driver()

            async def _noop_ttls(run):
                return None

            driver.refresh_artifact_ttls = _noop_ttls
            return await GraphDriver._settle_failed_dispatch(
                driver, run, object(), "charge-node", {}, exc, None
            )

        paused = await _settle(SideEffectReconciliationExhaustedError("budget spent"))
        failed = await _settle(RuntimeError("card declined"))

        assert paused.status is RunStatus.WAITING_INTERRUPT
        assert paused.failure_state is None
        assert failed.status is RunStatus.FAILED, "ordinary errors must still fail the run"
        assert failed.failure_state is not None
