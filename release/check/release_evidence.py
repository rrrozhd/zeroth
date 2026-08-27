"""Generate or validate identity-bound Zeroth Check V1 release evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from release.check.models import (
    REQUIRED_GATES,
    REQUIRED_SCHEMAS,
    AdapterEvidence,
    CheckReleaseEvidenceV1,
    GateEvidence,
    WheelEvidence,
)
from zeroth.check.tape.storage import atomic_write

ROOT = Path(__file__).parents[2]
EVIDENCE_PATH = Path("release/check/v1-release-evidence.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _one_wheel(directory: str | Path) -> Path:
    wheels = sorted(Path(directory).glob("zeroth_core-*.whl"))
    if len(wheels) != 1:
        raise ValueError("wheel directory must contain exactly one zeroth_core wheel")
    return wheels[0]


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain exactly one METADATA file")
        metadata = archive.read(metadata_names[0]).decode()
    versions = [
        line.removeprefix("Version: ")
        for line in metadata.splitlines()
        if line.startswith("Version: ")
    ]
    if len(versions) != 1:
        raise ValueError("wheel metadata must contain exactly one version")
    return versions[0]


def load_evidence(path: str | Path) -> CheckReleaseEvidenceV1:
    return CheckReleaseEvidenceV1.model_validate_json(Path(path).read_bytes())


def validate_artifact(evidence_path: str | Path, wheel_dir: str | Path) -> None:
    evidence = load_evidence(evidence_path)
    wheel = _one_wheel(wheel_dir)
    if wheel.name != evidence.wheel.filename or _sha256(wheel) != evidence.wheel.sha256:
        raise ValueError("released wheel does not match evidence identity")
    if _wheel_version(wheel) != evidence.package_version:
        raise ValueError("released wheel package version does not match evidence")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    required = {
        "zeroth/check/tape/models.py",
        "zeroth/check/faults/models.py",
        "zeroth/check/verdict/models.py",
        "zeroth/check/adapter/langgraph.py",
    }
    if not required <= names:
        raise ValueError("released wheel is missing Check V1 contract modules")


def validate_source(evidence_path: str | Path) -> None:
    evidence = load_evidence(evidence_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    if head == evidence.source_commit:
        return
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD^"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if parent != evidence.source_commit or changed != [EVIDENCE_PATH.as_posix()]:
        raise ValueError(
            "current source is neither the recorded commit nor its evidence-only child"
        )


def generate(wheel_dir: str | Path, output: str | Path) -> CheckReleaseEvidenceV1:
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    if dirty:
        raise ValueError("release evidence generation requires a clean implementation commit")
    wheel = _one_wheel(wheel_dir)
    gates = []
    for command in REQUIRED_GATES:
        completed = subprocess.run(command.split(), cwd=ROOT, check=False)
        if completed.returncode:
            raise RuntimeError(f"release gate failed: {command}")
        gates.append(
            GateEvidence(
                command=command,
                exit_status=0,
                completed_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
        )
    fixture_paths = sorted((ROOT / "tests/check/fixtures").rglob("*.json")) + sorted(
        (ROOT / "apps").glob("check_*/tapes/*.json")
    )
    package_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    evidence = CheckReleaseEvidenceV1(
        schema_version="check_release_evidence.v1",
        source_commit=subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip(),
        package_version=package_version,
        schemas=REQUIRED_SCHEMAS,
        adapter=AdapterEvidence(
            name="langgraph",
            version="1",
            dependency_version=importlib.metadata.version("langgraph"),
        ),
        wheel=WheelEvidence(filename=wheel.name, sha256=_sha256(wheel)),
        golden_fixture_sha256={
            str(path.relative_to(ROOT)): _sha256(path) for path in fixture_paths
        },
        gates=tuple(gates),
    )
    atomic_write(
        Path(output),
        json.dumps(evidence.model_dump(mode="json"), sort_keys=True, indent=2).encode() + b"\n",
    )
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-source", action="store_true")
    modes.add_argument("--check-artifact", action="store_true")
    parser.add_argument("--evidence", default=EVIDENCE_PATH)
    parser.add_argument("--wheel-dir")
    parser.add_argument("--output", default=EVIDENCE_PATH)
    args = parser.parse_args(argv)
    try:
        if args.check_source:
            validate_source(args.evidence)
        elif args.check_artifact:
            if not args.wheel_dir:
                raise ValueError("--check-artifact requires --wheel-dir")
            validate_artifact(args.evidence, args.wheel_dir)
        else:
            if not args.wheel_dir:
                raise ValueError("generation requires --wheel-dir")
            generate(args.wheel_dir, args.output)
    except (OSError, ValueError, RuntimeError, ValidationError) as exc:
        print(f"invalid release evidence: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
