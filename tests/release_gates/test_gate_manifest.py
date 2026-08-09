"""R2 — the manifest enumerates every required gate and artifact, and is itself checked."""

from __future__ import annotations

import json

import pytest

from .conftest import MANIFEST_PATH


def test_the_manifest_enumerates_every_gate_family_the_ticket_names(manifest):
    from gates.manifest import REQUIRED_GATES

    assert {gate["id"] for gate in manifest["gates"]} == REQUIRED_GATES


def test_every_gate_declares_its_artifacts_kinds_binds_and_triggers(manifest):
    for gate in manifest["gates"]:
        assert gate["requires"], f"{gate['id']} requires nothing"
        assert gate["kinds"], f"{gate['id']} produces no evidence kind"
        assert gate["binds"], f"{gate['id']} binds no identity"
        assert gate["triggers"], f"{gate['id']} runs at no trigger"
        assert gate["record"].startswith("release/evidence/")


def test_the_manifest_declares_every_acceptance_criteria_evidence_kind(manifest):
    from gates.manifest import REQUIRED_KINDS

    assert set(manifest["evidence_kinds"]) == REQUIRED_KINDS


def test_a_kind_that_is_not_applicable_must_say_why(manifest):
    """"As applicable" must never quietly mean "dropped"."""
    for name, kind in manifest["evidence_kinds"].items():
        if not kind["applicable"]:
            assert kind.get("reason"), f"{name} is not applicable and states no reason"


def test_gates_are_ordered_and_promotion_is_last(manifest):
    orders = [gate["order"] for gate in manifest["gates"]]

    assert orders == sorted(orders) == sorted(set(orders))
    assert manifest["gates"][-1]["id"] == "promotion"


def test_every_candidate_gate_precedes_the_final_gates(manifest):
    from gates.manifest import gates_for_phase

    candidate = gates_for_phase(manifest, "candidate")
    final = gates_for_phase(manifest, "final")

    assert [gate["id"] for gate in candidate] == [
        gate["id"] for gate in final if gate["phase"] == "candidate"
    ]
    assert max(gate["order"] for gate in candidate) < min(
        gate["order"] for gate in final if gate["phase"] == "final"
    )


def test_the_final_phase_covers_every_gate(manifest):
    from gates.manifest import gates_for_phase

    assert len(gates_for_phase(manifest, "final")) == len(manifest["gates"])


def test_manual_evidence_is_identified_as_operator_supplied(manifest):
    from gates.manifest import applicable_kinds

    assert applicable_kinds(manifest, "manual") == ["manual-signoff"]
    assert "manual-signoff" in dict(
        (gate["id"], gate["kinds"]) for gate in manifest["gates"]
    )["promotion"]


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda m: m["gates"].pop(), id="a-gate-removed"),
        pytest.param(lambda m: m.update(schema_version=99), id="unsupported-schema"),
        pytest.param(lambda m: m["evidence_kinds"].pop("sbom"), id="a-kind-removed"),
        pytest.param(
            lambda m: m["gates"][0].update(binds=["nonsense"]), id="unknown-identity-facet"
        ),
        pytest.param(lambda m: m["gates"][0].update(kinds=["undeclared"]), id="undeclared-kind"),
        pytest.param(lambda m: m["gates"][0].update(triggers=["whenever"]), id="unknown-trigger"),
        pytest.param(lambda m: m["gates"][0].update(requires=[]), id="requires-nothing"),
        pytest.param(lambda m: m["gates"].append(dict(m["gates"][0])), id="duplicate-gate"),
        pytest.param(
            lambda m: m["evidence_kinds"]["ui"].update(applicable=False),
            id="dropped-without-reason",
        ),
        pytest.param(lambda m: m.update(extra=True), id="unexpected-top-level-key"),
    ],
)
def test_a_malformed_manifest_is_rejected(tmp_path, mutation):
    """A manifest that silently lost a gate would turn this into an open system."""
    from gates.manifest import ManifestError, load_manifest

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    mutation(manifest)
    path = tmp_path / "release-gates.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ManifestError):
        load_manifest(path)


def test_an_unreadable_manifest_is_rejected(tmp_path):
    from gates.manifest import ManifestError, load_manifest

    path = tmp_path / "release-gates.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ManifestError):
        load_manifest(path)

    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "absent.json")
