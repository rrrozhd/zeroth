from __future__ import annotations

from hypothesis import given, strategies as st

from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint


BASE = {
    "case_id": "payment-case",
    "scenario_run_id": "logical-run-1",
    "tool_name": "charge_card",
    "input_schema_digest": "sha256:schema",
    "tool_call_id": "call-1",
    "argument_fingerprint": argument_fingerprint({"amount": 100, "currency": "USD"}),
}


def test_every_identity_field_changes_the_digest() -> None:
    original = action_identity_v1(**BASE)
    for field in BASE:
        changed = dict(BASE)
        changed[field] = f"{changed[field]}-changed"
        assert action_identity_v1(**changed) != original


def test_physical_execution_ids_are_not_accepted_or_used() -> None:
    first = action_identity_v1(**BASE)
    second = action_identity_v1(**BASE)
    assert first == second
    assert first.startswith("actv1_")


@given(st.text(min_size=1), st.text(min_size=1))
def test_distinct_case_and_run_pairs_have_distinct_identities(case_id: str, run_id: str) -> None:
    left = action_identity_v1(**(BASE | {"case_id": case_id, "scenario_run_id": run_id}))
    right = action_identity_v1(
        **(BASE | {"case_id": case_id + "x", "scenario_run_id": run_id + "x"})
    )
    assert left != right
