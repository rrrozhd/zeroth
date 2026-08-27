from __future__ import annotations

import sys
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.runner import CommandSpec, baseline_commands, execute_commands

ROOT = Path(__file__).parents[2]


def test_stateful_commands_use_distinct_empty_working_directories(tmp_path: Path) -> None:
    probe = (
        "from pathlib import Path; "
        "assert not Path('econ_plane.db').exists(); "
        "Path('owned-marker').write_text('ok')"
    )
    commands = [
        CommandSpec(name="first", argv=(sys.executable, "-c", probe), isolated=True),
        CommandSpec(name="second", argv=(sys.executable, "-c", probe), isolated=True),
    ]

    report = execute_commands(
        commands, artifact_root=tmp_path, evidence_store=EvidenceStore(tmp_path / "evidence")
    )

    assert report.passed
    assert [result.name for result in report.results] == ["first", "second"]
    assert report.results[0].working_directory != report.results[1].working_directory
    assert all(result.exit_code == 0 for result in report.results)


def test_runner_stops_after_the_first_failed_gate(tmp_path: Path) -> None:
    commands = [
        CommandSpec(
            name="fail",
            argv=(sys.executable, "-c", "raise SystemExit(7)"),
            isolated=True,
        ),
        CommandSpec(
            name="must-not-run",
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
            isolated=True,
        ),
    ]

    report = execute_commands(
        commands, artifact_root=tmp_path, evidence_store=EvidenceStore(tmp_path / "evidence")
    )

    assert not report.passed
    assert [result.name for result in report.results] == ["fail"]
    assert report.results[0].exit_code == 7


def test_baseline_plan_is_local_serial_and_never_selects_live_tests() -> None:
    commands = baseline_commands(ROOT)

    assert [command.name for command in commands] == [
        "studio-contracts",
        "budget-contracts",
        "evidence-contracts",
        "frontend-behavior",
        "frontend-production-build",
    ]
    assert all(command.isolated for command in commands[:3])
    assert all(not command.isolated for command in commands[3:])
    assert all("live" not in " ".join(command.argv) for command in commands)


def test_runner_refuses_to_execute_without_durable_evidence_store(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-execute"
    command = CommandSpec(
        name="unsafe-without-evidence",
        argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
    )

    with pytest.raises(ValueError, match="evidence_store"):
        execute_commands((command,), artifact_root=tmp_path)

    assert not marker.exists()


@pytest.mark.parametrize("name", ("../escape", "nested/name", "bad name"))
def test_runner_rejects_unsafe_command_name_before_execution(
    tmp_path: Path, name: str
) -> None:
    marker = tmp_path / "must-not-execute"
    command = CommandSpec(
        name=name,
        argv=(sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
    )

    with pytest.raises(ValueError, match="safe slug"):
        execute_commands(
            (command,),
            artifact_root=tmp_path,
            evidence_store=EvidenceStore(tmp_path / "evidence"),
        )

    assert not marker.exists()
