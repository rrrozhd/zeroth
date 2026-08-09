"""R8 — a human-readable verdict names every gate, and the bundle is retained."""

from __future__ import annotations

import json
import re
import subprocess
import sys

from .conftest import CLI, MANIFEST_PATH


def _render(manifest, candidate, evidence, phase="final"):
    from gates.validate import validate
    from gates.verdict import render

    results = validate(manifest, candidate, evidence, phase=phase)
    return render(manifest, candidate, results, phase=phase)


def _validate_one(manifest, candidate, evidence, gate_id, phase="final"):
    from gates.validate import validate

    results = validate(manifest, candidate, evidence, phase=phase)
    return next(result for result in results if result.gate == gate_id)


def test_the_verdict_names_every_gate_it_validated(manifest, candidate, evidence):
    document = _render(manifest, candidate, evidence)

    for gate in manifest["gates"]:
        assert gate["title"] in document, f"{gate['id']} is missing from the verdict"


def test_a_complete_candidate_reads_as_releasable(manifest, candidate, evidence):
    document = _render(manifest, candidate, evidence)

    assert document.startswith("# Release verdict: RELEASABLE")
    assert candidate["commit"] in document
    assert candidate["package"]["version"] in document


def test_a_blocked_candidate_states_the_gate_the_reason_and_the_artifact(
    manifest, candidate, evidence
):
    (evidence / "release/evidence/deployment-smoke.json").unlink()

    document = _render(manifest, candidate, evidence)

    assert document.startswith("# Release verdict: BLOCKED")
    assert "Deployment smoke" in document
    assert "missing" in document
    assert "release/evidence/deployment-smoke.json" in document


def test_the_verdict_records_the_candidate_identity_digest(manifest, candidate, evidence):
    from gates.identity import identity_digest

    assert identity_digest(candidate) in _render(manifest, candidate, evidence)


def test_the_verdict_names_the_evidence_no_ci_step_can_produce(manifest, candidate, evidence):
    assert "manual-signoff" in _render(manifest, candidate, evidence)


def test_the_verdict_command_writes_the_document_and_signals_the_outcome(
    candidate, evidence, tmp_path
):
    identity_path = tmp_path / "candidate-identity.json"
    identity_path.write_text(json.dumps(candidate), encoding="utf-8")
    output = tmp_path / "VERDICT.md"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CLI),
                "verdict",
                "--manifest",
                str(MANIFEST_PATH),
                "--identity",
                str(identity_path),
                "--evidence-root",
                str(evidence),
                "--phase",
                "final",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    passing = run()
    assert passing.returncode == 0, passing.stderr
    assert output.read_text(encoding="utf-8").startswith("# Release verdict: RELEASABLE")

    (evidence / "release/evidence/source.json").unlink()
    blocked = run()
    assert blocked.returncode == 1
    assert output.read_text(encoding="utf-8").startswith("# Release verdict: BLOCKED")


def test_a_pipe_in_a_reason_cannot_break_the_verdict_table(manifest, candidate, evidence):
    """Reasons quote identity values verbatim; a stray pipe must not split a row."""
    path = evidence / "release/evidence/deployment-smoke.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["identity"]["configuration"] = "sha256:tampered|value"
    path.write_text(json.dumps(record), encoding="utf-8")

    results = _validate_one(manifest, candidate, evidence, "deployment-smoke")
    assert results.status == "mismatched"
    assert "|" in results.reason, "this test only means something if the reason carries a pipe"

    document = _render(manifest, candidate, evidence)
    order = next(g["order"] for g in manifest["gates"] if g["id"] == "deployment-smoke")
    row = next(line for line in document.splitlines() if line.startswith(f"| {order} |"))

    # Four columns means five *unescaped* delimiters; the pipe inside the
    # reason must be escaped rather than opening a sixth cell.
    assert len(re.findall(r"(?<!\\)\|", row)) == 5, row
    assert r"\|" in row
