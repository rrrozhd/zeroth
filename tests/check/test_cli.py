from __future__ import annotations

import sys
from pathlib import Path

from zeroth.check.cli import _ensure_working_directory_on_import_path
from zeroth.check.tape.models import RawRecordingV1
from zeroth.service.cli import build_parser, main

from .replay.helpers import replay_tape
from .tape.test_models import _payload


def test_existing_and_check_commands_parse() -> None:
    assert build_parser().parse_args(["migrate"]).command == "migrate"
    record = build_parser().parse_args(["check", "record", "--case", "case-1"])
    assert record.check_command == "record"
    curate = build_parser().parse_args(
        ["check", "curate", "raw.json", "--reviewer", "reviewer", "--output", "tape.json"]
    )
    assert curate.check_command == "curate"


def test_console_script_adds_project_directory_for_spawned_target_rebuilds(
    monkeypatch,
) -> None:
    project = str(Path.cwd().resolve())
    monkeypatch.setattr(sys, "path", [item for item in sys.path if item not in {"", project}])

    _ensure_working_directory_on_import_path()

    assert sys.path[0] == project


def test_run_with_missing_config_fails_invalid(tmp_path) -> None:
    assert main(["check", "run", "--config", str(tmp_path / "missing")]) == 30


def test_curate_cli_writes_approved_tape(tmp_path) -> None:
    raw = RawRecordingV1.seal(**_payload())
    raw_path = tmp_path / "raw.json"
    raw_path.write_bytes(raw.canonical_bytes())
    output = tmp_path / "tape.json"
    assert (
        main(
            [
                "check",
                "curate",
                str(raw_path),
                "--reviewer",
                "reviewer",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.exists()


def test_record_config_error_returns_invalid(tmp_path) -> None:
    assert main(["check", "record", "--case", "case", "--config", str(tmp_path / "missing")]) == 30


def test_run_full_offline_check_and_explain_saved_verdict(tmp_path) -> None:
    tapes = tmp_path / "checks/tapes"
    tapes.mkdir(parents=True)
    (tapes / "case.json").write_bytes(replay_tape().canonical_bytes())
    config = tmp_path / "zeroth-check.yaml"
    config.write_text(
        """version: check.v1
target: tests.check.fixtures.targets.replay:build_target
tapes:
  curated_dir: checks/tapes
replay:
  runs: 3
  quorum: 2
faults:
  required: all
  additional: []
reporting:
  fail_on: [block, invalid]
"""
    )
    reports = tmp_path / "reports"
    assert (
        main(
            [
                "check",
                "run",
                "--config",
                str(config),
                "--report-dir",
                str(reports),
            ]
        )
        == 0
    )
    assert main(["check", "explain", str(reports / "check-verdict.json")]) == 0


def test_explain_rejects_a_tape_without_running_target(tmp_path) -> None:
    path = tmp_path / "tape.json"
    path.write_bytes(replay_tape().canonical_bytes())
    assert main(["check", "explain", str(path)]) == 30
