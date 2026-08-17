"""ZER-33 profile and CLI behavior contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "release/load/harness.py"
PROFILES = ROOT / "release/load/profiles-v1.json"
BASELINE = ROOT / "release/load/baseline-v1.json"


def test_profile_schema_fails_closed_on_unknown_or_unbounded_work() -> None:
    from release.load.report import ProfileError, validate_profiles

    valid = json.loads(PROFILES.read_text(encoding="utf-8"))
    invalid = json.loads(PROFILES.read_text(encoding="utf-8"))
    invalid["profiles"]["soak"]["duration_seconds"] = 0
    try:
        validate_profiles(invalid)
    except ProfileError as error:
        assert "duration_seconds" in str(error)
    else:  # pragma: no cover - makes a false pass impossible
        raise AssertionError("zero-duration soak profile was accepted")

    unknown = dict(valid)
    unknown["extra"] = True
    try:
        validate_profiles(unknown)
    except ProfileError as error:
        assert "unexpected" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unknown profile keys were accepted")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["matrix"].update(tenants=0),
        lambda value: value["surfaces"].append(value["surfaces"][0]),
        lambda value: value["faults"].update({"redis-loss": ""}),
        lambda value: value["thresholds"].update(derived_from="mutable.json"),
        lambda value: value["overload_contract"].update(statuses=[200]),
        lambda value: value["baseline"].update(path="mutable.json"),
    ],
)
def test_every_profile_control_field_is_validated(mutation) -> None:
    from release.load.report import ProfileError, validate_profiles

    value = json.loads(PROFILES.read_text(encoding="utf-8"))
    mutation(value)

    with pytest.raises(ProfileError):
        validate_profiles(value)


def test_cli_always_writes_machine_readable_failure_evidence(tmp_path: Path) -> None:
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": "a" * 40,
                "package": {"version": "0.23.10.2", "artifacts": {}},
            }
        ),
        encoding="utf-8",
    )
    observations = tmp_path / "rows.json"
    observations.write_text("[]\n", encoding="utf-8")
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            "run",
            "--profiles",
            str(PROFILES),
            "--baseline",
            str(BASELINE),
            "--identity",
            str(identity),
            "--observations",
            str(observations),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["errors"]


def test_cli_retains_machine_readable_evidence_when_an_input_is_malformed(tmp_path: Path) -> None:
    malformed = tmp_path / "baseline.json"
    malformed.write_text("{not-json\n", encoding="utf-8")
    identity = tmp_path / "identity.json"
    identity.write_text("{}\n", encoding="utf-8")
    observations = tmp_path / "rows.json"
    observations.write_text("[]\n", encoding="utf-8")
    output = tmp_path / "report.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            "run",
            "--profiles",
            str(PROFILES),
            "--baseline",
            str(malformed),
            "--identity",
            str(identity),
            "--observations",
            str(observations),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is False
