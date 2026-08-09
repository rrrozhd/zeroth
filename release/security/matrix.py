"""Strict, fail-closed loader for the ZER-32 security coverage matrix."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PROTECTED_SURFACES = frozenset(
    {
        "workflows",
        "deployments",
        "runs",
        "threads",
        "artifacts",
        "audit",
        "approvals",
        "connectors",
        "manifests",
        "repository-installations",
        "checkouts",
        "caches",
        "rate-limit-buckets",
        "execution-results",
    }
)
OPERATIONS = frozenset({"read", "write", "execute", "enumerate", "approve", "retrieve", "delete"})
PERSISTENCE_BOUNDARIES = frozenset(
    {"SQL", "Redis", "artifact-store", "LangGraph-checkpointer", "workers", "restarts"}
)
ATTACKS = frozenset(
    {
        "forged-tenant-claims",
        "identifier-guessing",
        "replay",
        "concurrency",
        "stale-credentials",
        "revoked-access",
    }
)
OBSERVABLE_OUTPUT_SURFACES = frozenset(
    {"workload-environment", "logs", "errors", "artifacts", "audit-payloads", "other-tenant"}
)
HOSTILE_FIXTURES = frozenset(
    {
        "traversal",
        "symlinks",
        "host-mounts",
        "network",
        "process-abuse",
        "resource-abuse",
        "output-abuse",
        "timeout",
        "cancellation",
    }
)
TIERS = frozenset({"pr-critical", "release-candidate"})
_MATRIX_KEYS = frozenset(
    {
        "schema_version",
        "protected_surfaces",
        "operations",
        "persistence_boundaries",
        "attacks",
        "observable_output_surfaces",
        "hostile_fixtures",
        "cases",
    }
)
_BEHAVIORAL_CASE_KEYS = frozenset({"id", "tiers", "coverage", "test_nodes"})
_ABSENCE_CASE_KEYS = _BEHAVIORAL_CASE_KEYS | {"refusal_test"}
_COVERAGE_KINDS = frozenset({"behavioral", "absent-fail-closed"})


class MatrixError(ValueError):
    """A matrix is malformed and must not be used as release evidence."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


class _StrictObject(dict[str, Any]):
    """A JSON object that retains duplicate keys for schema validation."""

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        counts = Counter(key for key, _ in pairs)
        self.duplicate_keys = tuple(key for key, _ in pairs if counts[key] > 1)
        super().__init__(pairs)


@dataclass(frozen=True)
class MatrixCase:
    """One executable security assertion and the tiers it protects."""

    id: str
    tiers: tuple[str, ...]
    coverage: str
    test_nodes: tuple[str, ...]
    refusal_test: str | None = None


@dataclass(frozen=True)
class Matrix:
    """The reviewed vocabulary and executable security coverage bindings."""

    protected_surfaces: tuple[str, ...]
    operations: tuple[str, ...]
    persistence_boundaries: tuple[str, ...]
    attacks: tuple[str, ...]
    observable_output_surfaces: tuple[str, ...]
    hostile_fixtures: tuple[str, ...]
    cases: tuple[MatrixCase, ...]

    def coverage_report(self) -> dict[str, tuple[str, ...]]:
        """Return absence proofs separately from ordinary behavioral coverage."""
        return {
            kind: tuple(case.id for case in self.cases if case.coverage == kind)
            for kind in sorted(_COVERAGE_KINDS)
        }


def _reject_unknown_fields(value: dict[str, Any], expected: frozenset[str], path: str) -> None:
    for key in dict.fromkeys(getattr(value, "duplicate_keys", ())):
        location = f"{path}.{key}" if path else key
        raise MatrixError(location, "duplicate field")
    for key in sorted(set(value) - expected):
        location = f"{path}.{key}" if path else key
        raise MatrixError(location, "unknown field")
    for key in sorted(expected - set(value)):
        location = f"{path}.{key}" if path else key
        raise MatrixError(location, "missing required field")


def _vocabulary(value: Any, path: str, expected: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise MatrixError(path, "must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise MatrixError(path, "must not contain duplicates")
    if frozenset(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        detail = []
        if missing:
            detail.append(f"missing required vocabulary: {', '.join(missing)}")
        if extra:
            detail.append(f"unknown vocabulary: {', '.join(extra)}")
        raise MatrixError(path, "; ".join(detail))
    return tuple(value)


def _test_nodes(value: Any, path: str, bound_nodes: set[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise MatrixError(path, "must bind at least one test node")
    nodes: list[str] = []
    for index, node in enumerate(value):
        node_path = f"{path}[{index}]"
        if not isinstance(node, str) or not node:
            raise MatrixError(node_path, "must be a non-empty node ID")
        if node in bound_nodes or node in nodes:
            raise MatrixError(node_path, "duplicates a test node binding")
        nodes.append(node)
    bound_nodes.update(nodes)
    return tuple(nodes)


def _case(value: Any, index: int, seen_ids: set[str], bound_nodes: set[str]) -> MatrixCase:
    path = f"cases[{index}]"
    if not isinstance(value, dict):
        raise MatrixError(path, "must be an object")
    coverage = value.get("coverage")
    if not isinstance(coverage, str) or coverage not in _COVERAGE_KINDS:
        raise MatrixError(f"{path}.coverage", "must be behavioral or absent-fail-closed")
    if coverage == "behavioral" and "refusal_test" in value:
        raise MatrixError(f"{path}.coverage", "absence proofs cannot be behavioral")
    _reject_unknown_fields(
        value,
        _ABSENCE_CASE_KEYS if coverage == "absent-fail-closed" else _BEHAVIORAL_CASE_KEYS,
        path,
    )
    identifier = value["id"]
    if not isinstance(identifier, str) or not identifier:
        raise MatrixError(f"{path}.id", "must be a non-empty string")
    if identifier in seen_ids:
        raise MatrixError(f"{path}.id", "duplicates a case ID")
    seen_ids.add(identifier)
    tiers = value["tiers"]
    if (
        not isinstance(tiers, list)
        or not tiers
        or any(not isinstance(tier, str) or tier not in TIERS for tier in tiers)
    ):
        raise MatrixError(f"{path}.tiers", "must contain known tiers")
    if len(tiers) != len(set(tiers)):
        raise MatrixError(f"{path}.tiers", "must not contain duplicates")
    if "pr-critical" in tiers and "release-candidate" not in tiers:
        raise MatrixError(f"{path}.tiers", "pr-critical cases must also be release-candidate cases")
    test_nodes = _test_nodes(value["test_nodes"], f"{path}.test_nodes", bound_nodes)
    refusal_test = value.get("refusal_test")
    if coverage == "absent-fail-closed":
        if not isinstance(refusal_test, str) or not refusal_test:
            raise MatrixError(f"{path}.refusal_test", "must bind a refusal test")
        if refusal_test not in test_nodes:
            raise MatrixError(f"{path}.refusal_test", "must be one of the case test nodes")
    return MatrixCase(identifier, tuple(tiers), coverage, test_nodes, refusal_test)


def load_matrix(path: Path) -> Matrix:
    """Load ``path`` or reject it before it can weaken a release gate."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_StrictObject)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MatrixError("$", f"matrix is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise MatrixError("$", "matrix must be an object")
    _reject_unknown_fields(value, _MATRIX_KEYS, "")
    if (
        not isinstance(value["schema_version"], int)
        or isinstance(value["schema_version"], bool)
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise MatrixError("schema_version", "unsupported schema version")
    fields = (
        ("protected_surfaces", PROTECTED_SURFACES),
        ("operations", OPERATIONS),
        ("persistence_boundaries", PERSISTENCE_BOUNDARIES),
        ("attacks", ATTACKS),
        ("observable_output_surfaces", OBSERVABLE_OUTPUT_SURFACES),
        ("hostile_fixtures", HOSTILE_FIXTURES),
    )
    vocabularies = {name: _vocabulary(value[name], name, required) for name, required in fields}
    if not isinstance(value["cases"], list) or not value["cases"]:
        raise MatrixError("cases", "must be a non-empty list")
    seen_ids: set[str] = set()
    bound_nodes: set[str] = set()
    cases = tuple(
        _case(case, index, seen_ids, bound_nodes) for index, case in enumerate(value["cases"])
    )
    represented_tiers = {tier for case in cases for tier in case.tiers}
    if represented_tiers != TIERS:
        raise MatrixError("cases", "must include both pr-critical and release-candidate coverage")
    return Matrix(cases=cases, **vocabularies)
