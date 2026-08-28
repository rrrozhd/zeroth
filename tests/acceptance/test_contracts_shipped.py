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


def _live_head(script_location: str) -> str:
    """The single head of a migration chain, as the running service would report it."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config("alembic.ini")
    config.set_main_option("script_location", script_location)
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected one head in {script_location}, got {heads}"
    return heads[0]


_SHIPPED = ("zeroth-v1.json", "zeroth-deployed-v1.json", "transport-conformance-v1.json")

_CHAINS = {
    "/health/ready": "src/zeroth/service/_migrations",
    "/regulus/health": "src/zeroth/econ/plane/_migrations",
}


def _pinned_revisions(name: str) -> dict[str, dict]:
    """Map each migrations-scenario path to the schema_revision it expects."""
    steps = _load(name)["scenarios"]["migrations"]["steps"]
    return {
        step["path"]: step["expected_json"]["schema_revision"]
        for step in steps
        if "schema_revision" in (step.get("expected_json") or {})
    }


@pytest.mark.parametrize("name", _SHIPPED)
def test_every_shipped_contract_pins_the_live_migration_heads(name: str) -> None:
    """A shipped contract must expect the head the service will actually report.

    These contracts are not merely fixtures: ``deployed-acceptance.yml`` and the
    ``release-zeroth-core.yml`` release job both run the CLI against a live
    deployment with ``--contract release/acceptance/contracts/<name>``. A stale pin
    therefore does not fail here -- it fails the release, against a service that is
    behaving correctly, at the one moment the gate is load-bearing.

    That is exactly what happened: the MCP migration renumber (027 -> 035) updated
    ``zeroth-v1.json`` and ``transport-conformance-v1.json`` and missed
    ``zeroth-deployed-v1.json``, which kept ``027`` and an econ head three
    migrations behind. Nothing caught it, because the only cross-contract equality
    test compares the ``gateway_http`` scenario and these pins live in
    ``migrations``. Resolving the head from the chain rather than hardcoding it
    means the next renumber cannot reintroduce the drift.
    """
    pinned = _pinned_revisions(name)
    assert set(pinned) == set(_CHAINS), f"{name} lost a migrations probe"
    for path, revision in pinned.items():
        head = _live_head(_CHAINS[path])
        assert revision["head"] == head, (
            f"{name} pins {path} head={revision['head']!r}, live chain head is {head!r}"
        )
        assert revision["applied"] == head, (
            f"{name} pins {path} applied={revision['applied']!r}, live chain head is {head!r}"
        )
        assert revision["state"] == "current"


def test_shipped_contracts_agree_on_the_migration_heads() -> None:
    """No shipped profile may expect a different schema head than its siblings."""
    revisions = {name: _pinned_revisions(name) for name in _SHIPPED}
    reference = revisions[_SHIPPED[0]]
    for name, pinned in revisions.items():
        assert pinned == reference, (
            f"{name} disagrees with {_SHIPPED[0]} on the migration heads: "
            f"{pinned} != {reference}"
        )
