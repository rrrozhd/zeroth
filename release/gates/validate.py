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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gates.identity import canonical, facet_matches, identity_digest
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

#: The shape each evidence kind must actually have. A kind absent here is only
#: required to be a non-empty file inside the evidence root.
EVIDENCE_SHAPES = {
    "junit": "junit",
    "ui": "junit",
    "compatibility": "json",
    "benchmark": "json",
    "sbom": "json",
    "provenance": "json",
    "security": "json",
}

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

DEPLOYED_ACCEPTANCE_SCENARIOS = frozenset(
    {
        "readiness",
        "authentication",
        "rbac",
        "migrations",
        "workflow_lifecycle",
        "deployment",
        "runs",
        "approvals",
        "audit",
        "artifacts",
        "retention",
        "gateway_http",
        "gateway_websocket",
        "compatibility",
        "executable_unit_failures",
        "restart_recovery",
        "shutdown",
    }
)


def _all_digests(values: Any) -> bool:
    return (
        isinstance(values, dict)
        and bool(values)
        and all(isinstance(item, str) and _DIGEST.match(item) for item in values.values())
    )


def _commit_reason(value: Any) -> str:
    if not isinstance(value, str) or not _COMMIT.match(value):
        return f"commit identity is not a commit sha: {value!r}"
    return ""


def _package_reason(value: Any) -> str:
    if not isinstance(value, dict) or not value.get("version"):
        return "package identity carries no version"
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return "package identity names no built artifact"
    if not _all_digests(artifacts):
        return "package identity artifacts are not sha256 digests"
    return ""


def _image_reason(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "image identity names no image"
    if not _all_digests(value):
        return "image identity values are not sha256 digests"
    return ""


def _digest_reason(facet: str, value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.match(value):
        return f"{facet} identity is not a sha256 digest: {value!r}"
    return ""


def _well_formed_facet(facet: str, value: Any) -> str:
    """Return why an identity facet is not a usable identity, or "".

    Equality alone is not identity: a candidate and a record that agree on an
    empty artifact map, or on a commit that is not a commit, would match while
    identifying nothing.
    """
    if facet == "commit":
        return _commit_reason(value)
    if facet == "package":
        return _package_reason(value)
    if facet == "image":
        return _image_reason(value)
    if facet in ("configuration", "compatibility"):
        return _digest_reason(facet, value)
    return ""


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


def _evidence_file_reason(kind: str, relative: str, gate: dict[str, Any], root: Path) -> str:
    """Return why a cited evidence file is not usable evidence, or "".

    Existence alone is far too weak. Without these checks a record could cite
    ``pyproject.toml`` -- or itself -- as its JUnit, SBOM and provenance
    evidence and be accepted, which turns the whole gate into paperwork.
    """
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        return f"{kind} evidence path must be relative to the evidence root: {relative}"
    path = root / relative
    if not path.is_file():
        return f"{kind} evidence file is absent: {relative}"
    if path.resolve() == (root / gate["record"]).resolve():
        return f"{kind} evidence cites the gate's own record: {relative}"
    if path.stat().st_size == 0:
        return f"{kind} evidence file is empty: {relative}"
    expected = EVIDENCE_SHAPES.get(kind)
    if expected is None:
        return ""
    try:
        head = path.read_text(encoding="utf-8", errors="replace").lstrip()[:4096]
    except OSError as error:
        return f"{kind} evidence file is unreadable: {error}"
    if expected == "json":
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return f"{kind} evidence must be JSON: {relative}"
    elif expected == "junit" and "<testsuite" not in head:
        return f"{kind} evidence must be a JUnit report: {relative}"
    return ""


def _kinds_reason(gate: dict[str, Any], record: dict[str, Any], root: Path) -> str:
    kinds = record["kinds"]
    if not isinstance(kinds, dict):
        return "record kinds must be an object"
    seen: dict[str, str] = {}
    for kind in gate["kinds"]:
        if kind not in kinds:
            return f"record is missing the {kind} evidence kind"
        relative = str(kinds[kind])
        reason = _evidence_file_reason(kind, relative, gate, root)
        if reason:
            return reason
        # One file standing in for several kinds means at least one of them was
        # never actually produced.
        if relative in seen:
            return f"{kind} evidence reuses the {seen[relative]} file: {relative}"
        seen[relative] = kind
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


def _binding_result(
    gate: dict[str, Any], identity: dict[str, Any], candidate: dict[str, Any]
) -> GateResult | None:
    """Return the stale/mismatched verdict, or None when the record binds this candidate."""
    if "commit" in gate["binds"] and not facet_matches(candidate, identity, "commit"):
        return GateResult(
            gate["id"],
            STALE,
            f"record is bound to commit {identity['commit']}, candidate is {candidate['commit']}",
        )
    for facet in gate["binds"]:
        if facet != "commit" and not facet_matches(candidate, identity, facet):
            return GateResult(
                gate["id"],
                MISMATCHED,
                f"record {facet} identity belongs to a different build "
                f"({canonical(identity[facet]).decode('utf-8')})",
            )
    return None


def _deployed_acceptance_result(
    gate: dict[str, Any],
    record: dict[str, Any],
    candidate: dict[str, Any],
    root: Path,
) -> GateResult | None:
    """Validate the semantic contents of ZER-35's deployment evidence."""
    if gate["id"] != "remote-acceptance":
        return None
    relative = record["kinds"].get("deployment")
    try:
        report = json.loads((root / str(relative)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return GateResult(gate["id"], PARTIAL, f"deployed acceptance report is unreadable: {error}")
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return GateResult(gate["id"], PARTIAL, "deployed acceptance report schema is invalid")
    if report.get("candidate_digest") != identity_digest(candidate):
        return GateResult(
            gate["id"],
            MISMATCHED,
            "deployed acceptance candidate digest belongs to a different build",
        )
    if canonical(report.get("image_identity")) != canonical(candidate.get("image")):
        return GateResult(
            gate["id"],
            MISMATCHED,
            "deployed acceptance image identity belongs to a different build",
        )
    tenant = report.get("tenant_id")
    namespace = report.get("namespace")
    if not isinstance(tenant, str) or not tenant.startswith("acceptance-"):
        return GateResult(gate["id"], PARTIAL, "report does not name a dedicated acceptance tenant")
    if not isinstance(namespace, str) or not namespace.startswith(f"{tenant}-"):
        return GateResult(gate["id"], PARTIAL, "report namespace is not owned by its tenant")
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list):
        return GateResult(gate["id"], PARTIAL, "report scenarios must be a list")
    names = {item.get("name") for item in scenarios if isinstance(item, dict) and item.get("name")}
    if len(scenarios) != len(DEPLOYED_ACCEPTANCE_SCENARIOS) or names != (
        DEPLOYED_ACCEPTANCE_SCENARIOS
    ):
        missing = sorted(DEPLOYED_ACCEPTANCE_SCENARIOS - names)
        return GateResult(
            gate["id"],
            PARTIAL,
            "report does not contain exactly the required scenarios; "
            f"missing: {', '.join(missing)}",
        )
    failed_scenarios = sorted(
        str(item.get("name"))
        for item in scenarios
        if not isinstance(item, dict) or item.get("status") != PASSED
    )
    if failed_scenarios:
        return GateResult(
            gate["id"], FAILED, f"deployed scenarios failed: {', '.join(failed_scenarios)}"
        )
    cleanup = report.get("cleanup")
    if not isinstance(cleanup, list) or not cleanup:
        return GateResult(gate["id"], PARTIAL, "report contains no cleanup evidence")
    if any(not isinstance(item, dict) or item.get("status") != PASSED for item in cleanup):
        return GateResult(gate["id"], FAILED, "deployed acceptance cleanup failed")
    compatibility = report.get("observed_compatibility")
    if not isinstance(compatibility, dict) or compatibility.get("status") != "supported":
        return GateResult(gate["id"], FAILED, "Agent Server compatibility is unsupported")
    if report.get("status") != PASSED:
        return GateResult(gate["id"], FAILED, f"deployed report status is {report.get('status')!r}")
    return None


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
    for facet in gate["binds"]:
        malformed = _well_formed_facet(facet, candidate[facet])
        if malformed:
            return GateResult(gate["id"], PARTIAL, f"candidate {malformed}")

    bound = _binding_result(gate, record["identity"], candidate)
    if bound is not None:
        return bound

    deployed = _deployed_acceptance_result(gate, record, candidate, evidence_root)
    if deployed is not None:
        return deployed

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
