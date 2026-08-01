"""Persistence tests for the ZER-8 decision record store (S4).

The repository owns the idempotency contract, so these tests exercise it
directly rather than through the service: a replayed key returns the stored
row (R3), a key reused for another action conflicts (R4), and the same key
under another tenant is a separate record (R16). The unique index on
``(tenant_id, idempotency_key)`` is the authority -- ``record`` inserts and
re-reads rather than checking first, so a concurrent duplicate resolves to the
stored row instead of a second write.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.governance.decisions import (
    DecisionKind,
    DecisionRepository,
    DecisionRequest,
    DecisionVerdict,
    IdempotencyConflictError,
    NormalizedAction,
    request_digest,
)

POLICY_VERSION = f"sha256:{'c' * 64}"


def make_request(**overrides: Any) -> DecisionRequest:
    """Build a complete decision request for tenant ``alpha``."""
    action_fields: dict[str, Any] = {
        "name": "send_email",
        "fingerprint": f"sha256:{'1' * 64}",
        "arguments_digest": f"sha256:{'2' * 64}",
        "contract_ref": "contracts/email@v1",
        "side_effect": "side_effecting",
    }
    action_fields.update(overrides.pop("action_overrides", {}))
    fields: dict[str, Any] = {
        "tenant_id": "tenant-alpha",
        "principal_id": "principal-alpha",
        "deployment_ref": "dep-alpha",
        "action": NormalizedAction(**action_fields),
        "idempotency_key": "key-alpha",
    }
    fields.update(overrides)
    return DecisionRequest(**fields)


def allow_verdict() -> DecisionVerdict:
    """An allow verdict carrying a concrete policy version."""
    return DecisionVerdict(
        kind=DecisionKind.ALLOW,
        reason_code="unknown_error",
        policy_version=POLICY_VERSION,
    )


async def record(repository: DecisionRepository, request: DecisionRequest) -> Any:
    """Persist ``request`` under an allow verdict with its own digest."""
    return await repository.record(
        request,
        digest=request_digest(request),
        verdict=allow_verdict(),
    )


async def test_a_recorded_decision_is_readable_by_its_key(sqlite_db: Any) -> None:
    """The stored row round-trips, digest included."""
    repository = DecisionRepository(sqlite_db)
    request = make_request()

    written = await record(repository, request)
    stored = await repository.find_by_idempotency_key("tenant-alpha", "key-alpha")

    assert stored is not None
    assert stored.response == written
    assert stored.request_digest == request_digest(request)
    assert stored.response.kind is DecisionKind.ALLOW
    assert stored.response.policy_version == POLICY_VERSION


async def test_an_unknown_key_reads_back_as_none(sqlite_db: Any) -> None:
    """A key nobody used is absent, not an empty decision."""
    repository = DecisionRepository(sqlite_db)

    assert await repository.find_by_idempotency_key("tenant-alpha", "nope") is None


async def test_recording_the_same_key_twice_returns_the_first_row(sqlite_db: Any) -> None:
    """R3: the insert is a no-op on conflict and the original row is re-served."""
    repository = DecisionRepository(sqlite_db)
    request = make_request()

    first = await record(repository, request)
    second = await record(repository, request)

    assert first.decision_id == second.decision_id
    assert first == second


async def test_recording_a_different_action_under_one_key_conflicts(sqlite_db: Any) -> None:
    """R4: the race path compares digests too, so it cannot serve the wrong verdict."""
    repository = DecisionRepository(sqlite_db)
    await record(repository, make_request())

    other = make_request(action_overrides={"name": "delete_account"})
    with pytest.raises(IdempotencyConflictError):
        await record(repository, other)


async def test_reading_a_key_with_a_mismatched_digest_conflicts(sqlite_db: Any) -> None:
    """R4: the read-side branch applies the same rule as the insert branch.

    Exercises ``find_replay`` rather than comparing two digests by hand: a test
    that only asserts the digests differ passes even when the comparison is
    removed from the repository, which is the mutation this must catch.
    """
    repository = DecisionRepository(sqlite_db)
    await record(repository, make_request())

    other = make_request(action_overrides={"name": "delete_account"})
    with pytest.raises(IdempotencyConflictError):
        await repository.find_replay(other, request_digest(other))


async def test_reading_a_key_with_a_matching_digest_re_serves_it(sqlite_db: Any) -> None:
    """The read-side branch still returns the original for an unchanged request."""
    repository = DecisionRepository(sqlite_db)
    request = make_request()
    written = await record(repository, request)

    assert await repository.find_replay(request, request_digest(request)) == written


async def test_the_same_key_under_two_tenants_is_two_records(sqlite_db: Any) -> None:
    """R16: tenant scoping is what stops one tenant's key from answering another's."""
    repository = DecisionRepository(sqlite_db)

    alpha = await record(repository, make_request(tenant_id="tenant-alpha"))
    beta = await record(repository, make_request(tenant_id="tenant-beta"))

    assert alpha.decision_id != beta.decision_id
    assert alpha.tenant_id == "tenant-alpha"
    assert beta.tenant_id == "tenant-beta"


async def test_a_lookup_never_crosses_a_tenant_boundary(sqlite_db: Any) -> None:
    """A key written by one tenant is invisible to another."""
    repository = DecisionRepository(sqlite_db)
    await record(repository, make_request(tenant_id="tenant-alpha"))

    assert await repository.find_by_idempotency_key("tenant-beta", "key-alpha") is None


async def test_an_approval_reference_round_trips(sqlite_db: Any) -> None:
    """A held decision keeps the approval it waits on."""
    repository = DecisionRepository(sqlite_db)
    request = make_request()

    written = await repository.record(
        request,
        digest=request_digest(request),
        verdict=DecisionVerdict(
            kind=DecisionKind.REQUIRE_APPROVAL,
            reason_code="policy_violation",
            policy_version=POLICY_VERSION,
            approval_ref="approval-7",
        ),
    )
    stored = await repository.find_by_idempotency_key("tenant-alpha", "key-alpha")

    assert written.approval_ref == "approval-7"
    assert stored is not None
    assert stored.response.approval_ref == "approval-7"
    assert stored.response.kind is DecisionKind.REQUIRE_APPROVAL
