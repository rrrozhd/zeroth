"""R3 — validation fails closed, with a distinct reason per rejection mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import COMMIT


def _validate(manifest, candidate, root: Path, phase: str = "final"):
    from gates.validate import validate

    return {result.gate: result for result in validate(manifest, candidate, root, phase=phase)}


def _gate(manifest, identifier):
    return next(gate for gate in manifest["gates"] if gate["id"] == identifier)


def _rewrite(root: Path, gate, mutate):
    path = root / gate["record"]
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_complete_current_bound_evidence_is_releasable(manifest, candidate, evidence):
    from gates.validate import releasable

    results = _validate(manifest, candidate, evidence)
    assert [result.status for result in results.values()] == ["passed"] * len(manifest["gates"])
    assert releasable(list(results.values()))


def test_absent_record_is_missing(manifest, candidate, evidence):
    gate = _gate(manifest, "package")
    (evidence / gate["record"]).unlink()

    result = _validate(manifest, candidate, evidence)["package"]

    assert result.status == "missing"
    assert result.blocking


def test_record_bound_to_an_earlier_commit_is_stale(manifest, candidate, evidence):
    gate = _gate(manifest, "source")
    _rewrite(evidence, gate, lambda record: record["identity"].update(commit="f" * 40))

    result = _validate(manifest, candidate, evidence)["source"]

    assert result.status == "stale"
    assert "f" * 40 in result.reason and COMMIT in result.reason


def test_record_not_covering_every_required_result_is_partial(manifest, candidate, evidence):
    gate = _gate(manifest, "untrusted-code")
    dropped = gate["requires"][0]
    _rewrite(evidence, gate, lambda record: record["results"].pop(dropped))

    result = _validate(manifest, candidate, evidence)["untrusted-code"]

    assert result.status == "partial"
    assert dropped in result.reason


def test_record_whose_evidence_file_is_absent_is_partial(manifest, candidate, evidence):
    gate = _gate(manifest, "langgraph")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    kind = gate["kinds"][0]
    (evidence / record["kinds"][kind]).unlink()

    result = _validate(manifest, candidate, evidence)["langgraph"]

    assert result.status == "partial"
    assert kind in result.reason


def test_record_not_binding_a_required_facet_is_partial(manifest, candidate, evidence):
    gate = _gate(manifest, "deployment-smoke")
    _rewrite(evidence, gate, lambda record: record["identity"].pop("image"))

    result = _validate(manifest, candidate, evidence)["deployment-smoke"]

    assert result.status == "partial"
    assert "image" in result.reason


def test_record_from_another_build_at_the_same_commit_is_mismatched(
    manifest, candidate, evidence
):
    """A rebuild at the same commit produces different bytes; that is not this candidate."""
    gate = _gate(manifest, "package")
    rebuilt = {
        "version": candidate["package"]["version"],
        "artifacts": dict.fromkeys(candidate["package"]["artifacts"], "sha256:" + "9" * 64),
    }
    _rewrite(evidence, gate, lambda record: record["identity"].update(package=rebuilt))

    result = _validate(manifest, candidate, evidence)["package"]

    assert result.status == "mismatched"


def test_record_reporting_a_failed_result_is_failed(manifest, candidate, evidence):
    gate = _gate(manifest, "source")
    _rewrite(evidence, gate, lambda record: record["results"].update(pytest="failed"))

    result = _validate(manifest, candidate, evidence)["source"]

    assert result.status == "failed"
    assert "pytest" in result.reason


def test_record_declaring_itself_failed_is_failed(manifest, candidate, evidence):
    gate = _gate(manifest, "remote-acceptance")
    _rewrite(evidence, gate, lambda record: record.update(status="failed"))

    result = _validate(manifest, candidate, evidence)["remote-acceptance"]

    assert result.status == "failed"


def test_a_stale_record_is_diagnosed_as_stale_even_when_it_also_failed(
    manifest, candidate, evidence
):
    """Lineage is diagnosed before outcome so the same tree always reports the same reason."""
    gate = _gate(manifest, "source")

    def mutate(record):
        record["identity"]["commit"] = "f" * 40
        record["status"] = "failed"

    _rewrite(evidence, gate, mutate)

    assert _validate(manifest, candidate, evidence)["source"].status == "stale"


def test_candidate_missing_a_bound_facet_cannot_pass(manifest, candidate, evidence):
    """No comparison is possible, and "no comparison" must never mean "passed"."""
    candidate.pop("image")

    result = _validate(manifest, candidate, evidence)["deployment-smoke"]

    assert result.status == "partial"
    assert "image" in result.reason


def test_an_arbitrary_file_is_not_evidence(manifest, candidate, evidence):
    """Citing any file that happens to exist would make the gate paperwork."""
    gate = _gate(manifest, "source")
    (evidence / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    _rewrite(evidence, gate, lambda record: record["kinds"].update(junit="pyproject.toml"))

    result = _validate(manifest, candidate, evidence)["source"]

    assert result.status == "partial"
    assert "JUnit" in result.reason


def test_a_record_cannot_cite_itself_as_its_own_evidence(manifest, candidate, evidence):
    gate = _gate(manifest, "remote-acceptance")
    _rewrite(
        evidence, gate, lambda record: record["kinds"].update(deployment=gate["record"])
    )

    result = _validate(manifest, candidate, evidence)["remote-acceptance"]

    assert result.status == "partial"
    assert "own record" in result.reason


def test_an_empty_evidence_file_is_not_evidence(manifest, candidate, evidence):
    gate = _gate(manifest, "untrusted-code")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    (evidence / record["kinds"]["junit"]).write_text("", encoding="utf-8")

    result = _validate(manifest, candidate, evidence)["untrusted-code"]

    assert result.status == "partial"
    assert "empty" in result.reason


@pytest.mark.parametrize("escape", ["/etc/hosts", "../../../etc/hosts"])
def test_evidence_cannot_escape_the_evidence_root(manifest, candidate, evidence, escape):
    gate = _gate(manifest, "source")
    _rewrite(evidence, gate, lambda record: record["kinds"].update(junit=escape))

    result = _validate(manifest, candidate, evidence)["source"]

    assert result.status == "partial"
    assert "relative to the evidence root" in result.reason


def test_one_file_cannot_stand_in_for_two_evidence_kinds(manifest, candidate, evidence):
    """Reuse means at least one of the two was never produced."""
    gate = _gate(manifest, "source")
    record = json.loads((evidence / gate["record"]).read_text(encoding="utf-8"))
    shared = record["kinds"]["junit"]
    _rewrite(evidence, gate, lambda item: item["kinds"].update(ui=shared))

    result = _validate(manifest, candidate, evidence)["source"]

    assert result.status == "partial"
    assert "reuses" in result.reason


def test_a_candidate_naming_no_built_artifact_cannot_pass(manifest, candidate, evidence):
    """Agreeing on an empty identity identifies nothing."""
    candidate["package"]["artifacts"] = {}

    result = _validate(manifest, candidate, evidence)["package"]

    assert result.status == "partial"
    assert "names no built artifact" in result.reason


@pytest.mark.parametrize(
    ("facet", "value"),
    [
        ("commit", "not-a-commit"),
        ("configuration", "whatever"),
        ("image", {}),
    ],
)
def test_a_malformed_candidate_identity_cannot_pass(manifest, candidate, evidence, facet, value):
    candidate[facet] = value
    gate_id = "source" if facet == "commit" else "deployment-smoke"

    result = _validate(manifest, candidate, evidence)[gate_id]

    assert result.status == "partial"


def test_an_empty_gate_set_is_not_releasable():
    from gates.validate import releasable

    assert releasable([]) is False


def test_candidate_phase_stops_before_the_final_gates(manifest, candidate, evidence):
    candidate_gates = set(_validate(manifest, candidate, evidence, phase="candidate"))
    final_gates = set(_validate(manifest, candidate, evidence, phase="final"))

    assert candidate_gates < final_gates
    assert {"remote-acceptance", "promotion"} == final_gates - candidate_gates


def test_unknown_phase_is_rejected(manifest, candidate, evidence):
    from gates.manifest import ManifestError

    with pytest.raises(ManifestError):
        _validate(manifest, candidate, evidence, phase="whenever")
