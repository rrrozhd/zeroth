from __future__ import annotations

from pathlib import Path

import pytest

from zeroth.check.config import load_check_config
from zeroth.check.tape.migrations import load_tape
from zeroth.service.cli import main

ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("name", ["check_payment", "check_approval"])
def test_reference_app_tape_is_approved_and_runs_offline(name: str, tmp_path) -> None:
    app = ROOT / "apps" / name
    config = load_check_config(app / "zeroth-check.yaml")
    tapes = list(config.tapes.curated_dir.glob("*.json"))
    assert len(tapes) == 1
    assert load_tape(tapes[0]).tool_occurrences[0].side_effect == "side_effecting"
    assert (
        main(
            [
                "check",
                "run",
                "--config",
                str(app / "zeroth-check.yaml"),
                "--report-dir",
                str(tmp_path / name),
            ]
        )
        == 0
    )
