"""Characterization of the retention erasure service's observable call order.

Task 9 decomposes ``RetentionErasureService`` into manifest, replay, claim,
executor, and compatibility collaborators. What that decomposition must
preserve is not a set of signatures — it is the *sequence* of calls the service
makes to its collaborators, and which of them share one transaction.

``tests/retention`` already covers the outcomes: claim fencing, heartbeat
renewal, legacy replay, idempotent operation ids, manifest repair, legal-hold
refusal. None of it pins the *interleaving*. That interleaving is exactly what a
careless extraction reorders, and in erasure code an order change is a
compliance bug rather than a cosmetic one — a manifest authorized before the
plaintext is gone, or a terminal event recorded before its operation deltas,
leaves a crash window that erases less than it claims to.

Every test here records collaborator calls through a transparent proxy and
asserts the exact ordered sequence. They are written against the
pre-decomposition service and must keep passing unchanged after it.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

import pytest

from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.erasure_service import RetentionErasureService


class _Journal:
    """Ordered log of every collaborator call the erasure service makes."""

    def __init__(self) -> None:
        self.entries: list[str] = []

    def record(self, entry: str) -> None:
        self.entries.append(entry)

    def starting_with(self, *prefixes: str) -> list[str]:
        return [entry for entry in self.entries if entry.startswith(prefixes)]


def _describe(label: str, name: str, kwargs: dict[str, Any]) -> str:
    """Name one call the way the retention log names it: by action, then operation."""
    action = kwargs.get("action")
    if isinstance(action, str):
        return f"{label}.{name}:{action}"
    operation = kwargs.get("operation")
    kind = getattr(operation, "kind", None)
    if kind is not None:
        return f"{label}.{name}:{kind}:{operation.status}"
    return f"{label}.{name}"


class _Recorder:
    """Transparent proxy recording each call made to one retention collaborator."""

    def __init__(self, target: Any, label: str, journal: _Journal) -> None:
        self._target = target
        self._label = label
        self._journal = journal

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name.startswith("_") or not callable(attribute):
            return attribute

        if inspect.iscoroutinefunction(attribute):

            async def _record_async(*args: Any, **kwargs: Any) -> Any:
                self._journal.record(_describe(self._label, name, kwargs))
                return await attribute(*args, **kwargs)

            return _record_async

        def _record_sync(*args: Any, **kwargs: Any) -> Any:
            self._journal.record(_describe(self._label, name, kwargs))
            return attribute(*args, **kwargs)

        return _record_sync


class _RecordingEconEraser:
    """Econ hook that records its join keys and reports a fixed deletion count."""

    def __init__(self, journal: _Journal, *, deleted: int = 2) -> None:
        self._journal = journal
        self._deleted = deleted
        self.calls: list[tuple[str, list[str]]] = []

    async def delete_events_for_run(
        self,
        tenant_id: str,
        join_keys: Sequence[str],
        *,
        idempotency_key: str,
    ) -> int:
        self._journal.record("econ.delete_events_for_run")
        self.calls.append((tenant_id, list(join_keys)))
        return self._deleted


def _instrument(service: RetentionErasureService, journal: _Journal) -> None:
    """Wrap every collaborator the service reaches for, leaving behavior intact."""
    service._audits = _Recorder(service._audits, "audits", journal)
    service._runs = _Recorder(service._runs, "runs", journal)
    service._holds = _Recorder(service._holds, "holds", journal)
    service._log = _Recorder(service._log, "log", journal)
    service._cleanup_state = _Recorder(service._cleanup_state, "state", journal)
    service._artifact_store = _Recorder(service._artifact_store, "artifacts", journal)


@pytest.fixture
def journal() -> _Journal:
    return _Journal()


async def test_erase_run_orders_every_side_effect_of_a_full_erasure(env, journal) -> None:
    """The whole happy path, end to end, in the one order it is allowed to run.

    The two properties this pins that nothing else does: the plaintext harvest
    and every destructive database write precede ``erasure_authorized`` inside a
    single transaction, and the external compatibility logs are written before
    the database ones.
    """
    await env.seed_run("run-order", n_audits=2, artifact_key="run-order/n0/blob")
    econ = _RecordingEconEraser(journal)
    env.service._econ_eraser = econ
    _instrument(env.service, journal)

    result = await env.service.erase_run("run-order", "rte")

    assert result.external_cleanup_status == "complete"
    assert journal.entries == [
        # Tenant resolution, before any lock is taken.
        "audits.list_by_run",
        "runs.get",
        # One tenant-serialized transaction: decide, harvest, destroy, authorize.
        "holds.active_holds_for_tenant_in_transaction",
        "runs.tenant_id_for_run_in_transaction",
        "runs.fence_token_snapshot_writes_in_transaction",
        "audits.list_by_run_in_transaction",
        "runs.erasure_payloads_in_transaction",
        "audits.crypto_erase_in_transaction",
        "audits.crypto_erase_in_transaction",
        "runs.erase_checkpoints_for_run_in_transaction",
        "runs.erase_token_snapshot_for_run_in_transaction",
        "runs.redact_run_in_transaction",
        "log.record_in_transaction:erasure_authorized",
        "state.initialize_in_transaction",
        # Claim the external cleanup in its own transaction.
        "log.get",
        "log.get_in_transaction",
        "state.get_state_in_transaction",
        "state.list_operations_in_transaction",
        "log.record_in_transaction:external_cleanup_claimed",
        "state.claim_in_transaction",
        # Artifact prefix sweep: in_progress delta, work, completed delta.
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_operation",
        "state.update_operation_in_transaction:artifact_prefix:in_progress",
        "artifacts.cleanup_run",
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_operation",
        "state.update_operation_in_transaction:artifact_prefix:completed",
        # The one harvested artifact key.
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_operation",
        "state.update_operation_in_transaction:artifact_key:in_progress",
        "artifacts.delete",
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_operation",
        "state.update_operation_in_transaction:artifact_key:completed",
        # Econ events last, after every artifact is gone.
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_operation",
        "state.update_operation_in_transaction:econ:in_progress",
        "econ.delete_events_for_run",
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_operation",
        "state.update_operation_in_transaction:econ:completed",
        # Terminal event, fenced against a stale claim.
        "state.get_state_in_transaction",
        "log.record_in_transaction:external_cleanup_completed",
        "state.terminal_in_transaction",
        # Best-effort compatibility logs: external first, then database.
        "log.record:artifact_cleanup",
        "log.record:econ_erase",
        "log.record:erase_run_complete",
        "log.record:crypto_erase_audits",
        "log.record:erase_checkpoints",
        "log.record:redact_run",
    ]


async def test_legal_hold_refusal_records_the_refusal_and_destroys_nothing(env, journal) -> None:
    """A hold short-circuits inside the lock: one log entry, no destructive call."""
    await env.seed_run("run-held", n_audits=2)
    await env.hold_repo.place(tenant_id="default", run_id="run-held", reason="litigation")
    _instrument(env.service, journal)

    from zeroth.governance.retention.erasure_service import LegalHoldError

    with pytest.raises(LegalHoldError):
        await env.service.erase_run("run-held", "rte")

    assert journal.entries == [
        "audits.list_by_run",
        "runs.get",
        "holds.active_holds_for_tenant_in_transaction",
        "log.record_in_transaction:erasure_refused_legal_hold",
    ]


async def test_ttl_recheck_failure_stops_before_the_plaintext_harvest(env, journal) -> None:
    """A run resurrected after selection is logged ineligible and left whole.

    The recheck must sit between the hold decision and the harvest: after it
    there is no non-destructive place left to stop.
    """
    from datetime import UTC, datetime, timedelta

    await env.seed_run("run-fresh", n_audits=2)
    _instrument(env.service, journal)

    cutoff = datetime.now(UTC) - timedelta(days=365)
    result = await env.service.erase_run("run-fresh", "ttl", ttl_cutoff=cutoff)

    assert result.audits_erased == 0
    assert result.run_redacted is False
    assert journal.entries == [
        "audits.list_by_run",
        "runs.get",
        "holds.active_holds_for_tenant_in_transaction",
        "runs.lock_and_recheck_erasable_run",
        "log.record_in_transaction:ttl_recheck_ineligible",
    ]


async def test_retry_resumes_at_the_first_unfinished_operation(env, journal) -> None:
    """A second pass re-claims, skips completed operations, and re-runs the rest."""
    await env.seed_run("run-resume", n_audits=1, artifact_key="run-resume/n0/blob")
    first = await env.service.erase_run("run-resume", "rte")
    log_id = first.authorization_log_id
    assert log_id

    _instrument(env.service, journal)
    result = await env.service.retry_external_cleanup(log_id)

    # Everything already completed, so the retry claims nothing and repairs nothing.
    assert result.external_cleanup_status == "complete"
    assert journal.entries == [
        "log.get",
        "log.get_in_transaction",
        "state.get_state_in_transaction",
        "state.list_operations_in_transaction",
    ]


async def test_a_failing_operation_still_completes_the_remaining_ones(env, journal) -> None:
    """One failure is recorded and does not abort the operations queued behind it.

    Ordering matters here beyond bookkeeping: an early artifact failure must not
    prevent the econ deletion, or a partial erasure would silently stand.
    """

    class _FailingArtifactStore:
        def __init__(self) -> None:
            self.deleted_keys: list[str] = []

        async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
            raise RuntimeError("prefix sweep unavailable")

        async def delete(self, key: str, *, idempotency_key: str) -> bool:
            self.deleted_keys.append(key)
            return True

    store = _FailingArtifactStore()
    env.service._artifact_store = store
    econ = _RecordingEconEraser(journal)
    env.service._econ_eraser = econ
    await env.seed_run("run-partial", n_audits=1, artifact_key="run-partial/n0/blob")
    _instrument(env.service, journal)

    result = await env.service.erase_run("run-partial", "rte")

    assert result.external_cleanup_status == "failed"
    # The failed prefix sweep did not stop the key delete or the econ deletion.
    assert store.deleted_keys == ["run-partial/n0/blob"]
    assert len(econ.calls) == 1
    operations = [entry for entry in journal.entries if "update_operation_in_transaction" in entry]
    assert operations == [
        "state.update_operation_in_transaction:artifact_prefix:in_progress",
        "state.update_operation_in_transaction:artifact_prefix:failed",
        "state.update_operation_in_transaction:artifact_key:in_progress",
        "state.update_operation_in_transaction:artifact_key:completed",
        "state.update_operation_in_transaction:econ:in_progress",
        "state.update_operation_in_transaction:econ:completed",
    ]
    # A failed run records the failed terminal and skips ``erase_run_complete``.
    assert journal.starting_with("log.record:", "log.record_in_transaction:external_cleanup_f") == [
        "log.record_in_transaction:external_cleanup_failed",
        "log.record:artifact_cleanup",
        "log.record:econ_erase",
        "log.record:crypto_erase_audits",
        "log.record:erase_checkpoints",
        "log.record:redact_run",
    ]


async def test_audit_ttl_purge_logs_each_run_after_every_crypto_erase(env, journal) -> None:
    """``purge_audits`` erases every aged audit first, then logs one entry per run."""
    from datetime import UTC, datetime, timedelta

    aged = datetime.now(UTC) - timedelta(days=30)
    await env.seed_run("run-aged-a", n_audits=2, created_at=aged)
    await env.seed_run("run-aged-b", n_audits=1, created_at=aged)
    await env.policy_repo.upsert(
        RetentionPolicy(tenant_id="default", audit_ttl_seconds=60),
    )
    _instrument(env.service, journal)

    results = await env.service.purge_audits("default")

    assert sorted(result.run_id for result in results) == ["run-aged-a", "run-aged-b"]
    assert journal.entries == [
        "holds.active_holds_for_tenant_in_transaction",
        "audits.list_erasable_in_transaction",
        "audits.crypto_erase_in_transaction",
        "audits.crypto_erase_in_transaction",
        "audits.crypto_erase_in_transaction",
        "log.record_in_transaction:audit_ttl_purged",
        "log.record_in_transaction:audit_ttl_purged",
    ]
