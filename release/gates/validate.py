"""Fail-closed validation of gate records against the candidate under release.

The ticket names five rejection reasons -- missing, stale, partial, mismatched,
failed -- and this module reports exactly those, one per gate, with the first
matching reason winning so a given evidence tree always produces the same
diagnosis.

Check order, and why it is this order:

1. ``missing``     -- no record at all. Nothing else can be said about it.
2. ``partial``     -- a record exists but does not cover what the manifest
   requires (missing fields, uncovered results, an evidence kind whose file is
   absent, or a facet the gate is supposed to bind but does not).
3. ``stale``       -- the record is complete but describes an earlier commit.
   Commit is the lineage marker, so it is diagnosed before digests: a record
   from last week's commit is stale, not mismatched, even though its artifact
   digests also differ.
4. ``mismatched``  -- same commit, but an identity facet binds a different
   digest. This is the "belongs to another build" case: a rebuild produces
   different artifact bytes at the same commit.
5. ``failed``      -- complete, current, correctly bound, and reporting that
   the gate did not pass.

A record is never trusted because it looks recent; ``generated_at`` is carried
for the human verdict and is deliberately *not* the staleness oracle. Wall
clocks can be wrong and a fresh timestamp on stale evidence is exactly the
thing this validator exists to reject.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gates.identity import canonical, facet_matches
from gates.manifest import select_gates

PASSED = "passed"
MISSING = "missing"
STALE = "stale"
PARTIAL = "partial"
MISMATCHED = "mismatched"
FAILED = "failed"

#: Every status a gate can be reported as. ``passed`` plus the five rejections.
STATUSES = (PASSED, MISSING, STALE, PARTIAL, MISMATCHED, FAILED)

RECORD_KEYS = frozenset(
    {"schema_version", "gate", "status", "identity", "results", "kinds", "generated_at"}
)
RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GateResult:
    """One gate's verdict against the candidate."""

    gate: str
    status: str
    reason: str

    @property
    def blocking(self) -> bool:
        """Whether this result must stop promotion."""
        return self.status != PASSED


def _load(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, f"no record at {path.as_posix()}"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"record at {path.as_posix()} is unreadable: {error}"
    if not isinstance(value, dict):
        return None, f"record at {path.as_posix()} is not an object"
    return value, ""


def _schema_reason(gate: dict[str, Any], record: dict[str, Any]) -> str:
    if set(record) != RECORD_KEYS:
        unexpected = sorted(set(record) ^ RECORD_KEYS)
        return f"record fields do not match the schema: {', '.join(unexpected)}"
    if record["schema_version"] != RECORD_SCHEMA_VERSION:
        return "record schema_version is unsupported"
    if record["gate"] != gate["id"]:
        return f"record declares gate {record['gate']!r}"
    if not isinstance(record["generated_at"], str) or not record["generated_at"]:
        return "record must state when it was generated"
    return ""


def _results_reason(gate: dict[str, Any], record: dict[str, Any]) -> str:
    results = record["results"]
    if not isinstance(results, dict):
        return "record results must be an object"
    required = set(gate["requires"])
    covered = set(results)
    if covered == required:
        return ""
    absent = sorted(required - covered)
    if absent:
        return f"record does not cover required results: {', '.join(absent)}"
    extra = ", ".join(sorted(covered - required))
    return f"record reports results the gate does not define: {extra}"


def _kinds_reason(gate: dict[str, Any], record: dict[str, Any], root: Path) -> str:
    kinds = record["kinds"]
    if not isinstance(kinds, dict):
        return "record kinds must be an object"
    for kind in gate["kinds"]:
        if kind not in kinds:
            return f"record is missing the {kind} evidence kind"
        if not (root / str(kinds[kind])).is_file():
            return f"{kind} evidence file is absent: {kinds[kind]}"
    return ""


def _binding_reason(gate: dict[str, Any], record: dict[str, Any]) -> str:
    identity = record["identity"]
    if not isinstance(identity, dict):
        return "record identity must be an object"
    absent = [facet for facet in gate["binds"] if facet not in identity]
    if absent:
        return f"record does not bind the {', '.join(absent)} identity this gate requires"
    return ""


def _structural_reason(gate: dict[str, Any], record: dict[str, Any], root: Path) -> str:
    """Return why the record is incomplete, or "" when it covers everything."""
    schema = _schema_reason(gate, record)
    if schema:
        # Every later check indexes fields this one has just proved present.
        return schema
    for reason in (
        _results_reason(gate, record),
        _kinds_reason(gate, record, root),
        _binding_reason(gate, record),
    ):
        if reason:
            return reason
    return ""


def validate_gate(
    gate: dict[str, Any], candidate: dict[str, Any], evidence_root: Path
) -> GateResult:
    """Validate one gate's record against the candidate identity."""
    record, reason = _load(evidence_root / gate["record"])
    if record is None:
        return GateResult(gate["id"], MISSING, reason)

    structural = _structural_reason(gate, record, evidence_root)
    if structural:
        return GateResult(gate["id"], PARTIAL, structural)

    # The candidate must itself carry every facet the gate binds; otherwise
    # there is nothing to compare against and "no comparison" must not pass.
    absent = [facet for facet in gate["binds"] if facet not in candidate]
    if absent:
        return GateResult(
            gate["id"],
            PARTIAL,
            f"candidate identity does not carry the {', '.join(absent)} facet this gate binds",
        )

    identity = record["identity"]
    if "commit" in gate["binds"] and not facet_matches(candidate, identity, "commit"):
        return GateResult(
            gate["id"],
            STALE,
            f"record is bound to commit {identity['commit']}, candidate is {candidate['commit']}",
        )
    for facet in gate["binds"]:
        if facet == "commit":
            continue
        if not facet_matches(candidate, identity, facet):
            return GateResult(
                gate["id"],
                MISMATCHED,
                f"record {facet} identity belongs to a different build "
                f"({canonical(identity[facet]).decode('utf-8')})",
            )

    if record["status"] != PASSED:
        return GateResult(gate["id"], FAILED, f"record status is {record['status']!r}")
    unpassed = sorted(name for name, value in record["results"].items() if value != PASSED)
    if unpassed:
        return GateResult(gate["id"], FAILED, f"results did not pass: {', '.join(unpassed)}")

    return GateResult(gate["id"], PASSED, "every required result passed and binds this candidate")


def validate(
    manifest: dict[str, Any],
    candidate: dict[str, Any],
    evidence_root: Path,
    *,
    phase: str = "final",
    trigger: str | None = None,
) -> list[GateResult]:
    """Validate every gate the phase and trigger require, in gate order."""
    return [
        validate_gate(gate, candidate, evidence_root)
        for gate in select_gates(manifest, phase=phase, trigger=trigger)
    ]


def releasable(results: list[GateResult]) -> bool:
    """Return whether every validated gate passed.

    An empty result set is never releasable: "no gates ran" is not "all gates
    passed", and treating it as success would be the open-by-default failure
    this whole module exists to prevent.
    """
    return bool(results) and all(not result.blocking for result in results)
