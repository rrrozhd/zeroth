"""Replaying a run's retention audit entries into current cleanup state.

This path only runs for authorizations written before the materialized state
table existed. It is pure -- rows in, state out -- but it is the only thing
standing between a legacy row and a wrong fencing decision, so the ordering and
generation rules are pinned directly here rather than through the service.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from zeroth.core.retention.cleanup_manifest import operation_id
from zeroth.governance.retention.replay import CleanupReplayState, replay_cleanup_state

TENANT = "default"
RUN = "run-legacy"
LOG_ID = "auth-log"
LEASE = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

PREFIX_ID = operation_id(TENANT, RUN, "artifact_prefix", RUN)
KEY_ID = operation_id(TENANT, RUN, "artifact_key", f"{RUN}/blob")
ECON_ID = operation_id(TENANT, RUN, "econ", RUN)


def _authorization(detail: dict | None = None) -> dict:
    manifest = detail or {
        "version": 1,
        "tenant_id": TENANT,
        "run_id": RUN,
        "reason": "rte",
        "database_result": {"audits_erased": 1, "checkpoints_deleted": 1, "run_redacted": True},
        "operations": [
            {
                "operation_id": PREFIX_ID,
                "kind": "artifact_prefix",
                "tenant_id": TENANT,
                "run_id": RUN,
            },
            {
                "operation_id": KEY_ID,
                "kind": "artifact_key",
                "tenant_id": TENANT,
                "run_id": RUN,
                "artifact_key": f"{RUN}/blob",
            },
            {
                "operation_id": ECON_ID,
                "kind": "econ",
                "tenant_id": TENANT,
                "run_id": RUN,
                "join_keys": [RUN],
            },
        ],
    }
    return {
        "log_id": LOG_ID,
        "tenant_id": TENANT,
        "run_id": RUN,
        "reason": "rte",
        "detail": json.dumps(manifest),
    }


def _entry(action: str, detail: dict, *, log_id: str = "entry") -> dict:
    return {"log_id": log_id, "action": action, "detail": json.dumps(detail)}


def _claim(*, claim_id: str, generation: int, revision: int, lease: datetime = LEASE) -> dict:
    return _entry(
        "external_cleanup_claimed",
        {
            "authorization_log_id": LOG_ID,
            "claim_id": claim_id,
            "generation": generation,
            "revision": revision,
            "lease_expires_at": lease.isoformat(),
        },
        log_id=f"claim-{claim_id}",
    )


def test_no_entries_yields_the_authorized_manifest_unclaimed() -> None:
    state = replay_cleanup_state(_authorization(), [])

    assert isinstance(state, CleanupReplayState)
    assert state.generation == 0
    assert state.revision == 0
    assert state.active_claim_id is None
    assert state.terminal_status is None
    assert [operation.status for operation in state.manifest.operations] == [
        "pending",
        "pending",
        "pending",
    ]


def test_entries_for_another_authorization_are_ignored() -> None:
    """A run may hold several authorizations; replay must not blend them."""
    foreign = _entry(
        "external_cleanup_claimed",
        {
            "authorization_log_id": "other-log",
            "claim_id": "c1",
            "generation": 9,
            "revision": 9,
            "lease_expires_at": LEASE.isoformat(),
        },
    )

    state = replay_cleanup_state(_authorization(), [foreign])

    assert state.generation == 0
    assert state.active_claim_id is None


def test_a_claim_becomes_the_active_lease() -> None:
    state = replay_cleanup_state(
        _authorization(), [_claim(claim_id="c1", generation=1, revision=1)]
    )

    assert state.generation == 1
    assert state.revision == 1
    assert state.active_claim_id == "c1"
    assert state.active_claim_log_id == "claim-c1"
    assert state.lease_expires_at == LEASE


def test_entries_replay_in_revision_order_not_list_order() -> None:
    """Rows arrive in log order; revision is the authority on what happened last."""
    later = _claim(claim_id="c2", generation=2, revision=2)
    earlier = _claim(claim_id="c1", generation=1, revision=1)

    state = replay_cleanup_state(_authorization(), [later, earlier])

    assert state.generation == 2
    assert state.active_claim_id == "c2"


def test_a_stale_generation_claim_never_supersedes_a_newer_one() -> None:
    """The fence: an expired worker's re-claim must not take the lease back."""
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c2", generation=2, revision=2),
            _claim(claim_id="c1", generation=1, revision=3),
        ],
    )

    assert state.generation == 2
    assert state.active_claim_id == "c2"


def test_a_heartbeat_from_the_active_claim_extends_the_lease() -> None:
    extended = LEASE + timedelta(seconds=30)
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c1", generation=1, revision=1),
            _entry(
                "external_cleanup_heartbeat",
                {
                    "authorization_log_id": LOG_ID,
                    "claim_id": "c1",
                    "generation": 1,
                    "revision": 2,
                    "lease_expires_at": extended.isoformat(),
                },
            ),
        ],
    )

    assert state.lease_expires_at == extended
    assert state.revision == 2


def test_a_heartbeat_from_a_foreign_claim_is_discarded() -> None:
    extended = LEASE + timedelta(seconds=30)
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c1", generation=1, revision=1),
            _entry(
                "external_cleanup_heartbeat",
                {
                    "authorization_log_id": LOG_ID,
                    "claim_id": "impostor",
                    "generation": 1,
                    "revision": 2,
                    "lease_expires_at": extended.isoformat(),
                },
            ),
        ],
    )

    assert state.lease_expires_at == LEASE


def test_an_operation_delta_updates_only_its_own_operation() -> None:
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c1", generation=1, revision=1),
            _entry(
                "external_cleanup_operation",
                {
                    "authorization_log_id": LOG_ID,
                    "claim_id": "c1",
                    "generation": 1,
                    "revision": 2,
                    "operation_id": KEY_ID,
                    "status": "completed",
                    "deleted_count": 1,
                },
            ),
        ],
    )

    statuses = {
        operation.operation_id: operation.status for operation in state.manifest.operations
    }
    assert statuses == {PREFIX_ID: "pending", KEY_ID: "completed", ECON_ID: "pending"}
    key = next(op for op in state.manifest.operations if op.operation_id == KEY_ID)
    assert key.deleted_count == 1


def test_a_delta_for_an_unknown_operation_is_rejected() -> None:
    """A manifest and its deltas disagreeing means the identity check failed upstream."""
    with pytest.raises(ValueError, match="unknown operation"):
        replay_cleanup_state(
            _authorization(),
            [
                _claim(claim_id="c1", generation=1, revision=1),
                _entry(
                    "external_cleanup_operation",
                    {
                        "authorization_log_id": LOG_ID,
                        "claim_id": "c1",
                        "generation": 1,
                        "revision": 2,
                        "operation_id": "not-in-this-manifest",
                        "status": "completed",
                    },
                ),
            ],
        )


def test_a_released_claim_leaves_the_state_reclaimable() -> None:
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c1", generation=1, revision=1),
            _entry(
                "external_cleanup_claim_released",
                {
                    "authorization_log_id": LOG_ID,
                    "claim_id": "c1",
                    "generation": 1,
                    "revision": 2,
                },
            ),
        ],
    )

    assert state.active_claim_id is None
    assert state.lease_expires_at is None
    assert state.generation == 1  # the generation is not rewound
    assert state.terminal_status is None


@pytest.mark.parametrize(
    ("action", "terminal"),
    [
        ("external_cleanup_completed", "completed"),
        ("external_cleanup_failed", "failed"),
    ],
)
def test_a_terminal_event_clears_the_claim_and_records_the_outcome(action, terminal) -> None:
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c1", generation=1, revision=1),
            _entry(
                action,
                {
                    "authorization_log_id": LOG_ID,
                    "claim_id": "c1",
                    "generation": 1,
                    "revision": 2,
                },
                log_id="terminal",
            ),
        ],
    )

    assert state.terminal_status == terminal
    assert state.terminal_log_id == "terminal"
    assert state.active_claim_id is None


def test_a_new_claim_clears_a_previous_terminal_log_id() -> None:
    """Re-claiming after a failed terminal must not report the stale terminal row."""
    state = replay_cleanup_state(
        _authorization(),
        [
            _claim(claim_id="c1", generation=1, revision=1),
            _entry(
                "external_cleanup_failed",
                {
                    "authorization_log_id": LOG_ID,
                    "claim_id": "c1",
                    "generation": 1,
                    "revision": 2,
                },
                log_id="terminal",
            ),
            _claim(claim_id="c2", generation=2, revision=3),
        ],
    )

    assert state.terminal_log_id is None
    assert state.active_claim_id == "c2"


def test_a_legacy_cleanup_status_entry_migrates_into_the_manifest() -> None:
    """Pre-manifest rows recorded progress as a ``cleanup_status`` blob."""
    legacy_authorization = _authorization(
        {
            "artifact_keys": [f"{RUN}/blob"],
            "join_keys": [RUN],
            "database_result": {
                "audits_erased": 2,
                "checkpoints_deleted": 1,
                "run_redacted": True,
            },
            # The authorization row records the manifest as it stood when written;
            # the later entry carries the progress made since.
            "cleanup_status": {
                "artifact_prefix": {"status": "pending", "deleted_count": 0},
                "artifact_keys": {f"{RUN}/blob": {"status": "pending", "deleted_count": 0}},
                "econ": {"status": "skipped", "deleted_count": None},
            },
        }
    )
    entry = _entry(
        "external_cleanup_completed",
        {
            "authorization_log_id": LOG_ID,
            "cleanup_status": {
                "artifact_prefix": {"status": "completed", "deleted_count": 1},
                "artifact_keys": {f"{RUN}/blob": {"status": "completed", "deleted_count": 1}},
                "econ": {"status": "skipped", "deleted_count": None},
            },
        },
    )

    state = replay_cleanup_state(legacy_authorization, [entry])

    assert all(
        operation.status in {"completed", "skipped"} for operation in state.manifest.operations
    )
