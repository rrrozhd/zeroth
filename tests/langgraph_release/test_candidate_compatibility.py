"""Current evidence must fail on drift without relabeling historical records."""

import json
from pathlib import Path

import pytest

from release.langgraph import candidate_compatibility as candidate
from release.gates.identity import file_digest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(candidate, "source_identity", lambda root: {"commit": "a" * 40})
    value = candidate.measure(ROOT)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(value))
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "commit": value["source"]["commit"],
                "package": {"version": value["release"]},
                "compatibility": file_digest(path),
            }
        )
    )
    return path, identity


def test_current_snapshot_verifies(snapshot):
    path, identity = snapshot
    candidate.verify(ROOT, path, identity)
    data = json.loads(path.read_text())
    assert "h2>=4.4.1,<5" in data["dependency_declarations"]["extras"]["langgraph-gateway"]
    assert data["supported_upstream"]["langgraph-api"] == "0.11.1"
    assert "passed" not in data


@pytest.mark.parametrize(
    "change", ["release", "installed", "declarations", "bytes", "missing", "source"]
)
def test_candidate_drift_is_rejected(snapshot, monkeypatch, change):
    path, identity = snapshot
    if change == "missing":
        path.unlink()
    elif change == "bytes":
        path.write_text(path.read_text() + "\n")
    elif change == "source":
        monkeypatch.setattr(candidate, "source_identity", lambda root: {"commit": "b" * 40})
    elif change == "installed":
        original = candidate.version
        monkeypatch.setattr(
            candidate, "version", lambda name: "0.0.0" if name == "h2" else original(name)
        )
    else:
        data = json.loads(path.read_text())
        if change == "release":
            data["release"] = "0.0.0"
        else:
            data["dependency_declarations"]["extras"]["langgraph-gateway"].remove("h2>=4.4.1,<5")
        path.write_text(json.dumps(data))
        bound = json.loads(identity.read_text())
        bound["compatibility"] = file_digest(path)
        identity.write_text(json.dumps(bound))
    with pytest.raises((ValueError, OSError, RuntimeError)):
        candidate.verify(ROOT, path, identity)


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error", "empty"])
def test_conformance_must_execute_without_skips(tmp_path, outcome):
    path = tmp_path / "junit.xml"
    case = "" if outcome == "empty" else f"<testcase><{outcome}/></testcase>"
    path.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
    with pytest.raises(ValueError):
        candidate.check_conformance(path)


def test_successful_conformance(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuites><testsuite><testcase name="ran"/></testsuite></testsuites>')
    candidate.check_conformance(path)


def test_failed_benchmark_is_rejected(tmp_path):
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps({"passed": False, "release": "0.25.7.3"}))
    with pytest.raises(ValueError):
        candidate.check_benchmark(path, "0.25.7.3")
