from __future__ import annotations

import hashlib
import json

import pytest

from zeroth.service import economic_diagnostic_cli as diagnostic_cli
from zeroth.service.economic_demo import generate_demo_pack


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_demo_writes_a_synthetic_closed_evidence_pack(tmp_path, capsys) -> None:
    output = tmp_path / "demo-pack"

    exit_code = diagnostic_cli.main(["demo", "--output-dir", str(output)])

    assert exit_code == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "README.md",
        "economic-diagnostic.json",
        "economic-diagnostic.md",
        "provider-reconciliation.json",
        "provider-reconciliation.md",
    ]
    pack_readme = (output / "README.md").read_text(encoding="utf-8")
    assert "Synthetic example — not customer evidence" in pack_readme
    assert "proven savings" not in pack_readme.lower()

    diagnostic = json.loads(
        (output / "economic-diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["decision_state"] == "economic_risk_observed"
    assert diagnostic["measured_failure_exposure_usd"] == 0.4
    assert diagnostic["top_failure_exposure"]["measured_repeated_attempt_cost_usd"] == 0.1

    reconciliation = json.loads(
        (output / "provider-reconciliation.json").read_text(encoding="utf-8")
    )
    assert reconciliation["reconciliation_state"] == "reconciled"
    assert reconciliation["billed_total_usd"] == "0.50000000"
    assert reconciliation["allocated_billed_usd"] == "0.50000000"
    assert reconciliation["unreconciled_billed_usd"] == "0E-8"
    assert reconciliation["outcome_unresolved_usd"] == "0"

    captured = capsys.readouterr()
    assert "Synthetic example — not customer evidence" in captured.out
    assert str(output.resolve()) in captured.out


def test_demo_refuses_to_overwrite_an_existing_pack(tmp_path, capsys) -> None:
    output = tmp_path / "demo-pack"
    assert diagnostic_cli.main(["demo", "--output-dir", str(output)]) == 0
    before = {path.name: _digest(path) for path in output.iterdir()}

    exit_code = diagnostic_cli.main(["demo", "--output-dir", str(output)])

    assert exit_code == 2
    assert {path.name: _digest(path) for path in output.iterdir()} == before
    assert "refusing to overwrite" in capsys.readouterr().err


def test_demo_defaults_to_a_named_output_directory() -> None:
    args = diagnostic_cli.build_parser().parse_args(["demo"])

    assert args.output_dir == "zeroth-economic-demo"


def test_demo_never_exposes_a_partial_pack_when_rendering_fails(tmp_path) -> None:
    output = tmp_path / "demo-pack"

    with pytest.raises(RuntimeError, match="render failed"):
        generate_demo_pack(
            output,
            render_diagnostic=lambda _report: (_ for _ in ()).throw(
                RuntimeError("render failed")
            ),
            render_reconciliation=lambda _report: "not reached",
        )

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []
