from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import release.live_evaluation as live_evaluation
from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.evidence import UnsafeEvidenceError


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "evaluation@example.invalid")
    _git(repository, "config", "user.name", "Evaluation Test")
    for name in ("modified.txt", "staged.txt", "deleted.txt"):
        (repository / name).write_text(f"committed {name}\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "baseline")
    return repository


def _campaign(tmp_path: Path) -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-control-plane",
            "tenant_id": "evaluation-control-plane",
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(tmp_path / "artifacts"),
            "action_sink_root": str(tmp_path / "artifacts" / "action-sink"),
        }
    )


def test_dirty_tree_hash_covers_each_git_state_and_is_deterministic(tmp_path: Path) -> None:
    dirty_tree_hash = getattr(live_evaluation, "dirty_tree_hash", None)
    assert dirty_tree_hash is not None
    repository = _repository(tmp_path)
    clean = dirty_tree_hash(repository)

    def restore() -> None:
        _git(repository, "reset", "--hard", "-q", "HEAD")
        untracked = repository / "untracked.txt"
        if untracked.exists():
            untracked.unlink()

    hashes: dict[str, str] = {}
    (repository / "modified.txt").write_text("modified but unstaged\n")
    hashes["modified"] = dirty_tree_hash(repository)
    restore()
    (repository / "staged.txt").write_text("modified and staged\n")
    _git(repository, "add", "staged.txt")
    hashes["staged"] = dirty_tree_hash(repository)
    restore()
    (repository / "untracked.txt").write_text("untracked\n")
    hashes["untracked"] = dirty_tree_hash(repository)
    assert hashes["untracked"] == dirty_tree_hash(repository)
    restore()
    (repository / "deleted.txt").unlink()
    hashes["deleted"] = dirty_tree_hash(repository)

    assert all(value.startswith("sha256:") for value in hashes.values())
    assert all(value != clean for value in hashes.values())
    assert len(set(hashes.values())) == len(hashes)


def test_capture_initializes_unique_bundle_and_defers_final_artifacts(tmp_path: Path) -> None:
    initialize = getattr(live_evaluation, "initialize_control_plane_evidence", None)
    assert initialize is not None
    repository = _repository(tmp_path)
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE witness (value TEXT NOT NULL)")
        connection.execute("INSERT INTO witness VALUES ('pretest')")

    arguments = {
        "evidence_root": tmp_path / "evidence",
        "repository_root": repository,
        "campaign": _campaign(tmp_path),
        "sqlite_sources": {"service": database},
        "runtime_versions": {"python": "3.12.11", "zeroth": "0.6.0"},
        "browser_versions": {"chromium": "140.0.0"},
        "container_versions": {"chroma": "chromadb/chroma:1.5.6"},
    }
    first = initialize(**arguments)
    second = initialize(**arguments)

    assert first.root != second.root
    assert first.root.parent == second.root.parent == tmp_path / "evidence"
    manifest = json.loads((first.root / "manifest.json").read_text())
    serialized_manifest = json.dumps(manifest, sort_keys=True)
    assert manifest["revision"] == _git(repository, "rev-parse", "HEAD")
    assert manifest["dirty_tree_hash"].startswith("sha256:")
    assert manifest["versions"]["browser"] == {"chromium": "140.0.0"}
    assert manifest["versions"]["containers"] == {"chroma": "chromadb/chroma:1.5.6"}
    assert manifest["campaign_config"]["campaign_id"] == "evaluation-control-plane"
    assert "provider_secret_ref" not in serialized_manifest
    assert "OPENAI_API_KEY" not in serialized_manifest

    snapshot = first.root / "database-snapshots" / "service-pretest.sqlite3"
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("SELECT value FROM witness").fetchone() == ("pretest",)
    assert first.acceptance_catalog
    assert {criterion.status for criterion in first.acceptance_catalog} == {"not_run"}
    assert not (first.root / "acceptance.json").exists()
    assert not (first.root / "report.md").exists()
    assert not (first.root / "SHA256SUMS").exists()

    with pytest.raises(UnsafeEvidenceError):
        first.finalize(
            report_markdown="# Unsafe\n\nAuthorization: Bearer credential-shaped-value\n",
            ledger=first.resume_ledger(),
        )
    assert not (first.root / "acceptance.json").exists()
    assert not (first.root / "report.md").exists()

    first.finalize(
        report_markdown="# Evaluation report\n\nAll acceptance gates remain not_run.\n",
        ledger=first.resume_ledger(),
    )

    assert (first.root / "acceptance.json").is_file()
    assert (first.root / "report.md").is_file()
    checksum_text = (first.root / "SHA256SUMS").read_text()
    assert "acceptance.json" in checksum_text
    assert "report.md" in checksum_text
    assert "database-snapshots/service-pretest.sqlite3" in checksum_text
    with pytest.raises(RuntimeError, match="sealed"):
        first.finalize(
            report_markdown="# replacement",
            ledger=first.resume_ledger(),
        )


def test_capture_can_attest_secret_bearing_sqlite_without_retaining_raw_snapshot(
    tmp_path: Path,
) -> None:
    initialize = getattr(live_evaluation, "initialize_control_plane_evidence", None)
    assert initialize is not None
    repository = _repository(tmp_path)
    database = tmp_path / "live.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE snapshots (snapshot_json TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO snapshots VALUES (?)",
            ('{"in_flight_dispatches":[{"token":"internal-control-value"}]}',),
        )

    evidence = initialize(
        evidence_root=tmp_path / "evidence",
        repository_root=repository,
        campaign=_campaign(tmp_path),
        sqlite_sources={"service": database},
        runtime_versions={"python": "3.12.11"},
        browser_versions={"safari": "26.5"},
        container_versions={"chroma": "chromadb/chroma:1.5.6"},
        retain_raw_snapshots=False,
    )

    manifest = json.loads((evidence.root / "manifest.json").read_text())
    assert "pretest_sqlite_snapshots" not in manifest
    assert manifest["pretest_sqlite_snapshot_attestations"] == (
        "database-snapshots/closed-snapshot-attestations.json"
    )
    attestation = json.loads(
        (evidence.root / manifest["pretest_sqlite_snapshot_attestations"]).read_text()
    )
    assert attestation["raw_snapshots_in_bundle"] is False
    assert attestation["snapshots"][0]["label"] == "service"
    assert attestation["snapshots"][0]["quick_check"] == "ok"
    assert len(attestation["snapshots"][0]["sha256"]) == 64
    assert not list((evidence.root / "database-snapshots").glob("*.sqlite3"))
    evidence.store.scan_recursive()
