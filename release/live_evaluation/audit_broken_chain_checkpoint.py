"""Seal disposable current-product broken-audit-chain rejection evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore

CRITERION = "audit.current-product-surface-broken-chain-rejection"
TENANT = "evaluation-studio-v1-twin"
DEPLOYMENT = "evaluation-studio-v1-twin-bootstrap-v1"
FAILED_AUDIT_ID = "service.authorization:35d04d87e12d4a318b261d6d09850e1a"


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) != 24:
        raise RuntimeError(f"invalid PNG checkpoint: {path}")
    return struct.unpack(">II", header[16:24])


def _contains(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, Mapping):
        return any(_contains(child, expected) for child in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains(child, expected) for child in value)
    return False


def _load_source(root: Path) -> tuple[dict[str, Any], list[Path]]:
    EvidenceStore(root).scan_recursive()
    results = json.loads((root / "results.json").read_text())
    criteria = results.get("criteria") if isinstance(results, Mapping) else None
    if results.get("completed") is not True or not isinstance(criteria, list):
        raise RuntimeError("Playwright evidence did not complete")
    matching = [
        row for row in criteria
        if isinstance(row, Mapping) and row.get("criterion_id") == CRITERION
    ]
    if len(matching) != 1 or matching[0].get("status") != "pass":
        raise RuntimeError("broken-chain criterion is not an exact pass")
    identities = list((root / "indexed").glob("*-response-identities.json"))
    if len(identities) != 2 or not all(
        _contains(json.loads(path.read_text()), FAILED_AUDIT_ID) for path in identities
    ):
        raise RuntimeError("both browser results must correlate the failed audit identity")
    screenshots = sorted((root / "artifacts").glob("*/audit-broken-chain-rejected.png"))
    sizes = {_png_size(path) for path in screenshots}
    if len(screenshots) != 2 or sizes != {(1440, 900), (2880, 1800)}:
        raise RuntimeError("checkpoint requires 1440x900 CSS captures at DPR 1 and DPR 2")
    return dict(results), screenshots


def _restored_verification(*, api_base: str, key_path: Path) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_base}/v1/deployments/{DEPLOYMENT}/audit-verification",
        headers={"X-API-Key": key_path.read_text().strip()},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)
    projected = {
        key: value.get(key)
        for key in (
            "scope", "verified", "record_count", "failed_audit_id", "error",
            "signature_verified", "signing_key_id", "unsigned_record_count",
        )
    }
    if projected["verified"] is not True or projected["signature_verified"] is not True:
        raise RuntimeError("disposable chain was not fully restored after the rejection proof")
    return projected


def _record_command(
    store: EvidenceStore,
    *,
    sequence: int,
    name: str,
    argv: list[str],
    cwd: Path,
) -> None:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    store.record_command(
        sequence=sequence,
        name=name,
        argv=argv,
        working_directory=cwd,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if result.returncode:
        raise RuntimeError(f"checkpoint command failed: {name}")


def build_checkpoint(
    *,
    source: Path,
    destination: Path,
    repository: Path,
    api_base: str,
    key_path: Path,
) -> Path:
    source = source.resolve(strict=True)
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    results, screenshots = _load_source(source)
    restored = _restored_verification(api_base=api_base, key_path=key_path)
    store = EvidenceStore(destination)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, capture_output=True, text=True, check=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repository, capture_output=True, check=True
    ).stdout
    store.write_manifest({
        "schema_version": 1,
        "checkpoint": "audit-broken-chain-ui-20260825-1",
        "created_at": datetime.now(UTC).isoformat(),
        "revision": revision,
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "tenant_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "roles": ["admin"],
        "browsers": ["Chromium", "WebKit"],
        "viewport": "1440x900",
        "provider_calls_performed": 0,
        "mutation_boundary": "Docker filesystem domain with both backend writers stopped",
        "cleanup_state": "original digest-matched record restored; chain and signature verified",
    })
    evidence: list[str] = []
    for source_path, relative in (
        (source / "results.json", Path("playwright-report/results.json")),
        (source / "html-report/index.html", Path("playwright-report/index.html")),
    ):
        store.ingest_artifact(source_path, relative)
        evidence.append(relative.as_posix())
    for row in results.get("artifacts", []):
        if not isinstance(row, Mapping):
            continue
        source_name, destination_name = row.get("source"), row.get("destination")
        if not isinstance(source_name, str) or not isinstance(destination_name, str):
            raise RuntimeError("invalid indexed Playwright artifact")
        if destination_name == "playwright-report/index.html":
            continue
        target = Path(destination_name)
        store.ingest_artifact(source / source_name, target)
        evidence.append(target.as_posix())
    screenshot_index = []
    for path in screenshots:
        browser = "WebKit" if "webkit" in path.parent.name else "Chromium"
        relative = Path("screenshots") / f"{browser.lower()}-broken-chain.png"
        store.ingest_artifact(path, relative)
        evidence.append(relative.as_posix())
        screenshot_index.append({
            "criterion_id": CRITERION,
            "route": "/audit",
            "control_id": "audit.verify-chain",
            "role": "admin",
            "tenant_id": TENANT,
            "viewport": "1440x900",
            "browser": browser,
            "pixel_size": "x".join(str(value) for value in _png_size(path)),
            "failed_audit_id": FAILED_AUDIT_ID,
            "expected_result": "chain rejected",
            "path": relative.as_posix(),
        })
    store._write_exclusive(Path("screenshots/index.json"), screenshot_index)
    evidence.append("screenshots/index.json")
    store._write_exclusive(Path("runtime/restored-verification.json"), restored)
    evidence.append("runtime/restored-verification.json")
    commands = (
        (
            "frontend-audit-tests",
            [
                "npm", "test", "--", "--run",
                "app/audit/page.test.tsx",
                "app/lib/secret-shapes.test.ts",
                "app/lib/evidence-reporter.test.ts",
            ],
            repository / "frontend",
        ),
        ("frontend-typecheck", ["npx", "tsc", "--noEmit"], repository / "frontend"),
        (
            "backend-audit-api-tests",
            ["uv", "run", "pytest", "tests/service/test_audit_api.py", "-q"],
            repository,
        ),
        ("diff-check", ["git", "diff", "--check"], repository),
    )
    for sequence, (name, argv, cwd) in enumerate(commands, start=1):
        _record_command(store, sequence=sequence, name=name, argv=argv, cwd=cwd)
        evidence.append(f"commands/{sequence:04d}-{name}.json")
    event_id = store.append_event(
        "campaign.audit.broken_chain_rejected_and_restored",
        {
            "result": "pass",
            "failed_audit_id": FAILED_AUDIT_ID,
            "digest_rejection": "record digest mismatch",
            "browsers": ["Chromium", "WebKit"],
            "provider_call_count": 0,
            "cleanup_state": "restored_and_verified",
        },
        correlation=CorrelationIds(audit_event_id=FAILED_AUDIT_ID),
    )
    evidence.append(f"events.ndjson#{event_id}")
    store.finalize_bundle(
        acceptance=(AcceptanceCriterion(CRITERION, "pass", tuple(evidence)),),
        report_markdown=(
            "# Current-product broken audit-chain rejection\n\n"
            "A disposable twin-tenant audit record was changed without recomputing its digest. "
            "The real Audit interface rejected the chain in Chromium and WebKit, named the exact "
            "failed record, and explained the digest mismatch. Both screenshots are 1440×900 and "
            "indexed to the criterion. Mutation and restoration ran only inside Docker's "
            "filesystem domain while both database writers were stopped. The original record "
            "was restored by a digest match; the final continuity and signature checks both pass. "
            "No provider call ran.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8123")
    parser.add_argument("--key-path", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        source=args.source,
        destination=args.destination,
        repository=args.repository,
        api_base=args.api_base,
        key_path=args.key_path,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
