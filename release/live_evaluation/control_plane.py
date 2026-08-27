"""Initialize and finalize reproducible live-evaluation evidence bundles."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import CampaignConfig
from .criteria import original_acceptance_criteria
from .evidence import AcceptanceCriterion, EvidenceStore
from .ledger import CampaignLedger

_SNAPSHOT_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ATTESTATION_PATH = "database-snapshots/closed-snapshot-attestations.json"


def _git(repository_root: Path, *args: str) -> bytes:
    return subprocess.run(
        ("git", *args),
        cwd=repository_root,
        check=True,
        capture_output=True,
    ).stdout


def _hash_part(digest: object, label: bytes, payload: bytes) -> None:
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def dirty_tree_hash(repository_root: Path) -> str:
    """Hash staged, unstaged, untracked, and deleted Git state deterministically."""
    root = repository_root.resolve(strict=True)
    digest = hashlib.sha256()
    _hash_part(
        digest,
        b"staged",
        _git(root, "diff", "--binary", "--no-ext-diff", "--cached", "HEAD", "--"),
    )
    _hash_part(
        digest,
        b"unstaged",
        _git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--"),
    )
    untracked = sorted(
        path
        for path in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if path
    )
    for relative_bytes in untracked:
        relative = Path(os.fsdecode(relative_bytes))
        path = root / relative
        payload = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        _hash_part(digest, b"untracked:" + relative_bytes, payload)
    return f"sha256:{digest.hexdigest()}"


def _head_revision(repository_root: Path) -> str:
    return _git(repository_root.resolve(strict=True), "rev-parse", "HEAD").decode().strip()


def _safe_campaign_config(campaign: CampaignConfig) -> dict[str, object]:
    return campaign.model_dump(mode="json", exclude={"provider_secret_ref"})


def _attest_sqlite_snapshot(source: Path, *, label: str) -> dict[str, object]:
    source = source.expanduser()
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"SQLite source is missing or unsafe: {label}")
    with tempfile.TemporaryDirectory(prefix="zeroth-control-snapshot-") as temporary:
        snapshot = Path(temporary) / f"{label}-pretest.sqlite3"
        try:
            with (
                sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as live,
                sqlite3.connect(snapshot) as copy,
            ):
                live.backup(copy)
            with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as database:
                quick_check = str(database.execute("PRAGMA quick_check").fetchone()[0])
                table_count = int(
                    database.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
                    ).fetchone()[0]
                )
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"SQLite snapshot failed: {label}") from exc
        if quick_check != "ok":
            raise RuntimeError(f"SQLite snapshot integrity failed: {label}")
        payload = snapshot.read_bytes()
        return {
            "label": label,
            "snapshot_name": snapshot.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "quick_check": "ok",
            "table_count": table_count,
            "raw_snapshot_in_bundle": False,
        }


@dataclass(frozen=True, slots=True)
class ControlPlaneEvidence:
    """An initialized bundle whose acceptance result remains memory-only until finalization."""

    store: EvidenceStore
    acceptance_catalog: tuple[AcceptanceCriterion, ...]

    @property
    def root(self) -> Path:
        return self.store.root

    def resume_ledger(self) -> CampaignLedger:
        """Rebuild acceptance state solely from the append-only event journal."""
        return CampaignLedger(self.store, self.acceptance_catalog)

    def finalize(
        self,
        *,
        report_markdown: str,
        ledger: CampaignLedger,
    ) -> None:
        """Publish the ledger-owned acceptance result and irreversibly seal once."""
        if ledger.store.root != self.store.root:
            raise ValueError("acceptance ledger belongs to a different evidence bundle")
        acceptance = ledger.resolved_criteria()
        expected_ids = tuple(item.criterion_id for item in self.acceptance_catalog)
        actual_ids = tuple(item.criterion_id for item in acceptance)
        if actual_ids != expected_ids:
            raise ValueError(
                "final acceptance catalog must preserve every seeded criterion in order"
            )
        self.store.validate(report_markdown)
        self.store.validate([asdict(item) for item in acceptance])
        self.store.finalize_bundle(
            acceptance=acceptance,
            report_markdown=report_markdown,
        )
        ledger.mark_finalized()


def initialize_control_plane_evidence(
    *,
    evidence_root: Path,
    repository_root: Path,
    campaign: CampaignConfig,
    sqlite_sources: Mapping[str, Path],
    runtime_versions: Mapping[str, str],
    browser_versions: Mapping[str, str],
    container_versions: Mapping[str, str],
    retain_raw_snapshots: bool = True,
) -> ControlPlaneEvidence:
    """Create one unique append-only bundle and capture its pretest control plane."""
    if not isinstance(campaign, CampaignConfig):
        raise TypeError("campaign must be an unresolved CampaignConfig")
    root = evidence_root.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(UTC)
    directory = root / (
        f"{campaign.campaign_id}-{captured_at.strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex}"
    )
    directory.mkdir(mode=0o700)
    store = EvidenceStore(directory)

    snapshots: list[str] = []
    attestations: list[dict[str, object]] = []
    for label, source in sorted(sqlite_sources.items()):
        if not _SNAPSHOT_LABEL.fullmatch(label):
            raise ValueError(f"unsafe SQLite snapshot label: {label!r}")
        if retain_raw_snapshots:
            name = f"{label}-pretest.sqlite3"
            store.snapshot_sqlite(source, name=name)
            snapshots.append(f"database-snapshots/{name}")
        else:
            attestations.append(_attest_sqlite_snapshot(source, label=label))

    snapshot_evidence: tuple[str, ...]
    if retain_raw_snapshots:
        snapshot_evidence = tuple(snapshots)
    else:
        store._write_exclusive(
            Path(_ATTESTATION_PATH),
            {
                "schema_version": 1,
                "raw_snapshots_in_bundle": False,
                "snapshots": attestations,
            },
        )
        snapshot_evidence = (_ATTESTATION_PATH,)

    manifest = {
        "campaign_config": _safe_campaign_config(campaign),
        "captured_at": captured_at.isoformat(),
        "dirty_tree_hash": dirty_tree_hash(repository_root),
        "revision": _head_revision(repository_root),
        "schema_version": 1,
        "versions": {
            "browser": dict(browser_versions),
            "containers": dict(container_versions),
            "runtime": dict(runtime_versions),
        },
    }
    if retain_raw_snapshots:
        manifest["pretest_sqlite_snapshots"] = snapshots
    else:
        manifest["pretest_sqlite_snapshot_attestations"] = _ATTESTATION_PATH
    store.write_manifest(manifest)
    ledger = CampaignLedger(store, original_acceptance_criteria())
    ledger.record(
        "control.revision-frozen",
        "pass",
        evidence=("manifest.json",),
    )
    ledger.record(
        "control.diff-hashed",
        "pass",
        evidence=("manifest.json",),
    )
    ledger.record(
        "control.database-snapshots",
        "pass",
        evidence=snapshot_evidence,
    )
    return ControlPlaneEvidence(
        store=store,
        acceptance_catalog=original_acceptance_criteria(),
    )
