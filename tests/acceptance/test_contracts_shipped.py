"""Guards for the shipped acceptance contracts (audit P1, release gate).

The CLI-driven deployed acceptance run installs an HttpLifecycleController whose
stop_upstream raises (a deployed platform exposes no upstream-removal controller),
so the required ``stop_upstream -> 502 -> start_upstream`` triad in gateway_http
made the remote gate impossible to pass. The fix splits the contract by profile:
the shipped ``zeroth-v1.json`` keeps the full triad (the ephemeral leg's
authoritative 502 proof), a new ``zeroth-deployed-v1.json`` omits only that
lifecycle triad under ``profile: "deployed"``, and the 502 requirement is gated on
``profile == "full"``. These tests pin all of that so neither the ephemeral proof
nor the deployed relaxation can silently drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.acceptance.models import AcceptanceContract

_CONTRACTS = Path("release/acceptance/contracts")


def _load(name: str) -> dict:
    return json.loads((_CONTRACTS / name).read_text(encoding="utf-8"))


def test_shipped_full_contract_keeps_the_502_lifecycle_triad() -> None:
    contract = AcceptanceContract.model_validate(_load("zeroth-v1.json"))
    assert contract.profile == "full"  # default; the ephemeral leg runs the full proof
    steps = contract.scenarios["gateway_http"].steps
    assert any(step.protocol == "lifecycle" for step in steps), "stop/start_upstream must remain"
    assert any(step.expected_status == 502 for step in steps), "the 502 proof must remain"


def test_deployed_contract_validates_without_the_502_triad() -> None:
    contract = AcceptanceContract.model_validate(_load("zeroth-deployed-v1.json"))
    assert contract.profile == "deployed"
    steps = contract.scenarios["gateway_http"].steps
    assert all(step.protocol != "lifecycle" for step in steps), "no lifecycle step for deployed"
    assert not any(step.expected_status == 502 for step in steps)
    # The relaxation is scoped to the 502 lifecycle step only — admit + deny stay.
    assert any(200 <= (step.expected_status or 0) < 300 for step in steps)
    assert any(step.expected_status == 403 for step in steps)


def test_deployed_and_full_cover_the_same_scenarios() -> None:
    full = AcceptanceContract.model_validate(_load("zeroth-v1.json"))
    deployed = AcceptanceContract.model_validate(_load("zeroth-deployed-v1.json"))
    assert set(deployed.scenarios) == set(full.scenarios)


def test_deployed_gateway_http_is_a_strict_subset_of_full() -> None:
    full = _load("zeroth-v1.json")["scenarios"]["gateway_http"]["steps"]
    deployed = _load("zeroth-deployed-v1.json")["scenarios"]["gateway_http"]["steps"]
    assert deployed == [s for s in full if s.get("protocol") != "lifecycle" and s.get("expected_status") != 502]


def test_full_profile_still_demands_the_502_proof() -> None:
    # Forcing the deployed (502-less) gateway_http to the full profile must fail —
    # the gate is not vacuous.
    raw = _load("zeroth-deployed-v1.json")
    raw["profile"] = "full"
    with pytest.raises(ValueError, match="upstream failure"):
        AcceptanceContract.model_validate(raw)
