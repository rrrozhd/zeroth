"""Review and reseal legacy control-gate evidence without raw secret-bearing databases."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from release.live_evaluation.evidence import AcceptanceCriterion, EvidenceStore

CONTROL_CRITERIA = (
    "control.revision-frozen",
    "control.diff-hashed",
    "control.database-snapshots",
    "control.audit-signed",
    "control.tenant-budget-10",
    "control.run-budget-025",
    "control.budget-concurrency",
    "control.budget-rejection",
    "control.budget-commit-release",
    "control.budget-recovery",
    "control.chroma-pinned-loopback",
    "stop.no-ambiguous-auto-retry",
    "economics.campaign-and-run-caps",
    "stop.cost-cap-enforced",
)
_PROMOTED_CAP_CRITERIA = {
    "economics.campaign-and-run-caps",
    "stop.cost-cap-enforced",
}
_BUDGET_COMMANDS = (
    "commands/0006-budget-concurrency.json",
    "commands/0007-budget-commit-release.json",
    "commands/0008-budget-rejection.json",
    "commands/0009-budget-recovery.json",
)


def _validated_checksums(root: Path) -> dict[str, str]:
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise RuntimeError("source checksum manifest is missing")
    expected: dict[str, str] = {}
    for line in manifest.read_text().splitlines():
        digest, separator, relative_text = line.partition("  ")
        relative = Path(relative_text)
        if (
            not separator
            or len(digest) != 64
            or relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
        ):
            raise RuntimeError("source checksum manifest is malformed")
        target = root / relative
        if not target.is_file() or target.is_symlink():
            raise RuntimeError(f"source checksum target is missing: {relative_text}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            raise RuntimeError(f"source checksum mismatch: {relative_text}")
        expected[relative.as_posix()] = digest
    return expected


def _snapshot_attestation(path: Path, *, relative: str, digest: str) -> dict[str, object]:
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
            quick_check = str(connection.execute("pragma quick_check").fetchone()[0])
            table_count = int(
                connection.execute(
                    "select count(*) from sqlite_master where type = 'table'"
                ).fetchone()[0]
            )
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"source SQLite snapshot is invalid: {relative}") from exc
    if quick_check != "ok":
        raise RuntimeError(f"source SQLite snapshot quick_check failed: {relative}")
    return {
        "source_relative_path": relative,
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "quick_check": quick_check,
        "table_count": table_count,
        "raw_snapshot_in_sealed_bundle": False,
    }


def _validated_budget_commands(source_root: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for relative in _BUDGET_COMMANDS:
        record = json.loads((source_root / relative).read_text())
        if record.get("exit_code") != 0 or str(record.get("stderr", "")):
            raise RuntimeError(f"budget command did not pass: {relative}")
        stdout = json.loads(str(record.get("stdout", "")))
        if not isinstance(stdout, dict):
            raise RuntimeError(f"budget command output is invalid: {relative}")
        records[relative] = stdout
    if int(records[_BUDGET_COMMANDS[0]].get("admitted_count", 0)) < 1:
        raise RuntimeError("budget concurrency proof is incomplete")
    commit = records[_BUDGET_COMMANDS[1]]
    if "committed_usd" not in commit or "released_usd" not in commit:
        raise RuntimeError("budget commit/release proof is incomplete")
    rejection = records[_BUDGET_COMMANDS[2]]
    if not rejection.get("run_overage_rejected") or not rejection.get("tenant_overage_rejected"):
        raise RuntimeError("budget rejection proof is incomplete")
    recovery = records[_BUDGET_COMMANDS[3]]
    if not recovery.get("recovered") or not recovery.get("terminal_state_reconciled"):
        raise RuntimeError("budget recovery proof is incomplete")
    return records


def build_checkpoint(*, source_root: Path, destination_root: Path) -> Path:
    source_root = source_root.resolve(strict=True)
    checksums = _validated_checksums(source_root)
    source_manifest = json.loads((source_root / "manifest.json").read_text())
    source_acceptance = json.loads((source_root / "acceptance.json").read_text())
    _validated_budget_commands(source_root)
    snapshot_paths = source_manifest.get("pretest_sqlite_snapshots")
    if not isinstance(snapshot_paths, list) or not snapshot_paths:
        raise RuntimeError("source manifest has no pre-test SQLite snapshots")

    attestations: list[dict[str, object]] = []
    for value in snapshot_paths:
        relative = Path(str(value))
        relative_text = relative.as_posix()
        if relative.is_absolute() or ".." in relative.parts or relative_text not in checksums:
            raise RuntimeError("source SQLite snapshot inventory is invalid")
        attestations.append(
            _snapshot_attestation(
                source_root / relative,
                relative=relative_text,
                digest=checksums[relative_text],
            )
        )

    rows = {
        str(row.get("criterion_id")): row
        for row in source_acceptance.get("criteria", [])
        if isinstance(row, dict)
    }
    missing = [
        criterion_id
        for criterion_id in CONTROL_CRITERIA
        if criterion_id not in _PROMOTED_CAP_CRITERIA
        and rows.get(criterion_id, {}).get("status") != "pass"
    ]
    if missing:
        raise RuntimeError(f"source control criteria are not passing: {missing}")

    store = EvidenceStore(destination_root)
    reviewed_manifest = {
        key: value for key, value in source_manifest.items() if key != "pretest_sqlite_snapshots"
    }
    reviewed_manifest.update(
        {
            "checkpoint": destination_root.name,
            "source_bundle": source_root.name,
            "source_bundle_checksum_count": len(checksums),
            "pretest_sqlite_snapshot_attestations": (
                "database-snapshots/closed-snapshot-attestations.json"
            ),
            "raw_snapshots_in_sealed_bundle": False,
        }
    )
    store.write_manifest(reviewed_manifest)
    store._write_exclusive(
        Path("database-snapshots/closed-snapshot-attestations.json"),
        {
            "schema_version": 1,
            "source_bundle": source_root.name,
            "source_checksum_manifest_verified": True,
            "raw_snapshots_in_sealed_bundle": False,
            "snapshots": attestations,
        },
    )
    events_payload = (source_root / "events.ndjson").read_bytes()
    store._atomic_bytes_exclusive(Path("events.ndjson"), events_payload)
    for relative_text in _BUDGET_COMMANDS:
        relative = Path(relative_text)
        store._atomic_bytes_exclusive(relative, (source_root / relative).read_bytes())

    criteria: list[AcceptanceCriterion] = []
    for criterion_id in CONTROL_CRITERIA:
        row = rows[criterion_id]
        evidence = tuple(str(item) for item in row.get("evidence", []))
        if criterion_id == "control.database-snapshots":
            evidence = ("database-snapshots/closed-snapshot-attestations.json",)
        elif criterion_id in _PROMOTED_CAP_CRITERIA:
            evidence = _BUDGET_COMMANDS
        criteria.append(
            AcceptanceCriterion(
                criterion_id=criterion_id,
                status="pass",
                evidence=evidence,
                note=(
                    "Reviewed against the checksum-verified source bundle; raw SQLite snapshots "
                    "remain external because credential-bearing columns are prohibited in "
                    "sealed evidence."
                    if criterion_id == "control.database-snapshots"
                    else (
                        "Promoted from four exact passing budget command proofs after the original "
                        "downstream probe blocker was separated from cap enforcement."
                        if criterion_id in _PROMOTED_CAP_CRITERIA
                        else "Reviewed from the checksum-verified source control-gate bundle."
                    )
                ),
            )
        )
    store.validate_evidence_references(criteria)
    store.write_acceptance(criteria)
    store.write_report(
        "# Sanitized control-gate checkpoint\n\n"
        "The original bundle's checksums and SQLite integrity were verified. Raw snapshots are "
        "not copied because their operational schemas contain credential-bearing columns. This "
        "bundle contains only hashes, sizes, integrity results, table counts, and the original "
        "secret-clean control events.\n"
    )
    store.scan_recursive()
    store.write_checksums()
    return destination_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    args = parser.parse_args()
    build_checkpoint(source_root=args.source_root, destination_root=args.destination_root)


if __name__ == "__main__":
    main()
