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
