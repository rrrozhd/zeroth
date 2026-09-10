"""G3 reviewer package integrity: every row and required failure has a result; nothing is accepted."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).parent
ROWS = ("C01", "C02", "C03", "C04", "C05", "C06", "C07")
FAILURES = (
    "unknown model or pricing", "denied key (401)", "unavailable telemetry backend", "truncated stream",
    "cancelled request", "event replay", "conflicting event identity", "missing outcome", "tenant mismatch",
)


def manifest() -> dict:
    return json.loads((HERE / "manifest.json").read_text())


def test_frozen_inputs_still_match():
    m = manifest()
    for name, digest in m["frozen"].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name


def test_every_row_has_a_recipe_evidence_and_no_acceptance():
    m = manifest()
    root = HERE.parents[2]
    for row in ROWS:
        record = m["rows"][row]
        assert (root / record["recipe"] / "README.md").exists(), row
        assert "NOT an accepted" in record["status"], row
        assert record["reviewer"] == "pending independent review", row
        for key in ("identity_mapping", "cost_categories", "supported_modes", "excluded_modes", "frameworks", "runtime"):
            assert record[key], (row, key)
        evidence = record["evidence"]
        for path in evidence.get("clean_install", []) + ([evidence["file"]] if "file" in evidence else []):
            assert (HERE / path).exists(), (row, path)
        assert (HERE / evidence["tests"].split(" ")[0]).exists(), row
    assert all(value == "not accepted" for value in m["acceptance"].values())
    assert m["reviewer_decision"] == "pending independent review"


def test_required_failures_cover_every_row_and_not_run_legs_name_their_inputs():
    m = manifest()
    assert set(m["required_failures"]) == set(FAILURES)
    for failure, cells in m["required_failures"].items():
        assert set(cells) == set(ROWS) | {"contract"}, failure
        assert all(isinstance(result, str) and result for result in cells.values()), failure
    for leg, record in m["not_run"].items():
        assert record["status"] == "NOT_RUN" and record["needs"], leg
    assert "live_provider_evidence" in m["not_run"] and "independent_recipe_reviewer" in m["not_run"]


def test_candidate_identity_and_lifecycle_are_recorded():
    m = manifest()
    assert m["candidate"]["base"].startswith("faa16232") and m["candidate"]["branch"] == "acceptance/phase-3-integrations"
    assert m["candidate"]["commits_since_base"]
    assert isinstance(m["lifecycle"]["overhead_per_call_ms"], float) and m["lifecycle"]["overhead_method"]
    assert (HERE / m["lifecycle"]["evidence"]).exists()
