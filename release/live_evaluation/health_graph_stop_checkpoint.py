"""Seal the derived all-workflow exact-health stop checkpoint.

The checkpoint never invokes a provider or browser.  It accepts only three
previously sealed source bundles and derives ``stop.health-matches-graph`` only
after their checksums and exact workflow health assertions validate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, EvidenceStore

STATE_ROOT = Path(
    "/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1"
)
ROOT = STATE_ROOT / "evidence/health-graph-stop-checkpoint-20260824-1"
SOURCE_ROOTS = {
    "workflow1": STATE_ROOT / "evidence/workflow1-local-20260824-1",
    "workflow2": STATE_ROOT / "evidence/graph-repair-live-20260824-1",
    "workflow3": STATE_ROOT / "evidence/workflow3-v4-checkpoint-20260824-2",
}

_EXPECTED_HEALTH: Mapping[str, Mapping[str, object]] = {
    "workflow1": {
        "campaign_id": "evaluation-studio-v1",
        "deployment_ref": "evaluation-studio-v1-grounded-researcher-v1",
        "deployment_version": 5,
        "graph_version_ref": "evaluation-studio-v1-grounded-researcher@3",
        "status": "ok",
    },
    "workflow2": {
        "deployment_ref": "evaluation-studio-v1-batched-investigation-parent-v1",
        "deployment_version": 2,
        "graph_version_ref": "evaluation-studio-v1-batched-investigation-parent@2",
        "status": "ok",
    },
    "workflow3": {
        "campaign_id": "evaluation-studio-v1",
        "deployment_ref": "evaluation-studio-v1-governed-remediation-v2",
        "deployment_version": 3,
        "graph_version_ref": "evaluation-studio-v1-governed-remediation@4",
        "status": "ok",
    },
}
_WORKFLOW3_RESTART_ARGV = [
    "docker",
    "compose",
    "-f",
    "compose.dev.yml",
    "up",
    "-d",
    "--force-recreate",
    "backend",
]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid source JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid source JSON object: {path.name}")
    return payload


def _verify_checksums(root: Path) -> str:
    root = root.resolve(strict=True)
    checksum_path = root / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"missing source checksum manifest: {root.name}") from exc
    declared: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise RuntimeError(f"malformed source checksum manifest: {root.name}")
        digest, relative_text = parts
        relative = relative_text.lstrip("* ")
        candidate = (root / relative).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"unsafe source checksum path: {root.name}") from exc
        if relative in declared:
            raise RuntimeError(f"duplicate source checksum path: {root.name}")
        declared[relative] = digest

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if set(declared) != actual_files:
        raise RuntimeError(f"source checksum inventory mismatch: {root.name}")
    for relative, expected in declared.items():
        actual = sha256((root / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"source checksum mismatch: {root.name}/{relative}")
    return sha256(checksum_path.read_bytes()).hexdigest()


def _criterion(root: Path, workflow: str) -> dict[str, Any]:
    payload = _load_json(root / "results.json")
    rows = payload.get("criteria")
    if not isinstance(rows, list):
        raise RuntimeError(f"missing source criteria: {root.name}")
    criterion_id = f"{workflow}.health-exact-graph-version"
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("criterion_id") == criterion_id
    ]
    if len(matches) != 1 or matches[0].get("status") != "pass":
        raise RuntimeError(f"exact health assertion did not pass: {workflow}")
    return matches[0]


def _event_health(root: Path, workflow: str, criterion: Mapping[str, Any]) -> dict[str, Any]:
    evidence = criterion.get("evidence")
    if not isinstance(evidence, list) or len(evidence) != 1:
        raise RuntimeError(f"exact health evidence is incomplete: {workflow}")
    prefix = "events.ndjson#"
    reference = evidence[0]
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise RuntimeError(f"exact health event reference is invalid: {workflow}")
    event_id = reference.removeprefix(prefix)
    matches: list[dict[str, Any]] = []
    try:
        lines = (root / "events.ndjson").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"missing exact health events: {workflow}") from exc
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid exact health events: {workflow}") from exc
        if isinstance(event, dict) and event.get("event_id") == event_id:
            matches.append(event)
    if (
        len(matches) != 1
        or matches[0].get("type") != "campaign.deployment.health_verified"
        or matches[0].get("data") != _EXPECTED_HEALTH[workflow]
    ):
        raise RuntimeError(f"exact health event did not match: {workflow}")
    return matches[0]["data"]


def _workflow3_health(root: Path, criterion: Mapping[str, Any]) -> dict[str, Any]:
    expected_evidence = {
        "console/health-v4.json",
        "commands/0004-backend-docker-restart-v4.json",
    }
    evidence = criterion.get("evidence")
    if not isinstance(evidence, list) or set(evidence) != expected_evidence:
        raise RuntimeError("exact health evidence is incomplete: workflow3")
    health = _load_json(root / "console/health-v4.json")
    command = _load_json(root / "commands/0004-backend-docker-restart-v4.json")
    if health != _EXPECTED_HEALTH["workflow3"]:
        raise RuntimeError("exact health result did not match: workflow3")
    if (
        command.get("exit_code") != 0
        or command.get("name") != "backend-docker-restart-v4"
        or command.get("argv") != _WORKFLOW3_RESTART_ARGV
    ):
        raise RuntimeError("exact health restart did not match: workflow3")
    return health


def _validate_sources(source_roots: Mapping[str, Path]) -> dict[str, dict[str, Any]]:
    if set(source_roots) != set(_EXPECTED_HEALTH):
        raise RuntimeError("exactly three workflow health source roots are required")
    summaries: dict[str, dict[str, Any]] = {}
    for workflow in _EXPECTED_HEALTH:
        root = source_roots[workflow].resolve(strict=True)
        checksum_manifest_sha256 = _verify_checksums(root)
        criterion = _criterion(root, workflow)
        health = (
            _workflow3_health(root, criterion)
            if workflow == "workflow3"
            else _event_health(root, workflow, criterion)
        )
        summaries[workflow] = {
            "source_root": root.name,
            "source_checksum_manifest_sha256": checksum_manifest_sha256,
            "source_criterion_id": criterion["criterion_id"],
            "health": health,
        }
    return summaries


def build_checkpoint(*, destination: Path, source_roots: Mapping[str, Path]) -> Path:
    """Validate immutable inputs, then append and seal the composite checkpoint."""
    if destination.exists():
        raise RuntimeError(f"checkpoint already exists: {destination}")
    summaries = _validate_sources(source_roots)
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "all-workflow-exact-health-derived-stop",
            "created_at": datetime.now(UTC).isoformat(),
            "derivation": {
                "criterion_id": "stop.health-matches-graph",
                "required_source_criteria": [
                    f"{workflow}.health-exact-graph-version"
                    for workflow in _EXPECTED_HEALTH
                ],
            },
            "source_roots": {
                workflow: summary["source_root"]
                for workflow, summary in summaries.items()
            },
        }
    )
    evidence: list[str] = []
    for workflow, summary in summaries.items():
        reference = Path(f"sources/{workflow}.json")
        store._write_exclusive(reference, summary)
        evidence.append(reference.as_posix())
    event_id = store.append_event(
        "campaign.stop.health_matches_graph.derived",
        {
            "result": "pass",
            "source_count": len(summaries),
            "source_criteria": [
                summary["source_criterion_id"] for summary in summaries.values()
            ],
        },
    )
    report = """# Exact workflow health stop checkpoint

All three immutable source bundles passed checksum verification. Their explicit
workflow health assertions identify the exact deployed graph versions recorded
for Workflow 1, Workflow 2, and the current Workflow 3 v4 deployment. This
provider-free derivation therefore passes `stop.health-matches-graph` and no
other campaign criterion.
"""
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion(
                "stop.health-matches-graph",
                "pass",
                tuple(evidence),
            ),
        ),
        report_markdown=report,
    )
    # The derivation event is retained for auditability but is deliberately not
    # the acceptance proof: the three copied source summaries are the proof.
    del event_id
    return destination


def main() -> int:
    root = build_checkpoint(destination=ROOT, source_roots=SOURCE_ROOTS)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
