"""Contract tests for the fail-closed security coverage matrix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.security.matrix import (
    ATTACKS,
    HOSTILE_FIXTURES,
    OBSERVABLE_OUTPUT_SURFACES,
    OPERATIONS,
    PERSISTENCE_BOUNDARIES,
    PROTECTED_SURFACES,
    TIERS,
    MatrixError,
    load_matrix,
)


FIXTURE = Path(__file__).parent / "fixtures" / "valid-matrix.json"
PRODUCTION = Path(__file__).parents[2] / "release" / "security" / "security-matrix.json"


def _write_matrix(tmp_path: Path, mutate) -> Path:
    matrix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutate(matrix)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    return path


def _write_production_matrix(tmp_path: Path, mutate) -> Path:
    matrix = json.loads(PRODUCTION.read_text(encoding="utf-8"))
    mutate(matrix)
    path = tmp_path / "production-matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "dimension",
    (
        "protected_surfaces",
        "operations",
        "persistence_boundaries",
        "attacks",
        "observable_output_surfaces",
        "hostile_fixtures",
    ),
)
def test_production_claims_require_every_dimension(tmp_path: Path, dimension: str) -> None:
    with pytest.raises(MatrixError) as raised:
        load_matrix(
            _write_production_matrix(tmp_path, lambda matrix: matrix["claims"].pop(dimension))
        )
    assert raised.value.path == f"claims.{dimension}"


def test_production_claims_reject_one_ceremonial_node_collapse(tmp_path: Path) -> None:
    ceremonial = "tests/security/test_matrix_contract.py::test_fixture_behavioral_binding"

    def collapse(matrix) -> None:
        for claims in matrix["claims"].values():
            for value in claims:
                claims[value] = ceremonial

    with pytest.raises(MatrixError) as raised:
        load_matrix(_write_production_matrix(tmp_path, collapse))
    assert raised.value.path == "claims.protected_surfaces.workflows"


def test_production_observable_claims_bind_exact_canary_scanners() -> None:
    matrix = load_matrix(PRODUCTION)

    assert matrix.claims["observable_output_surfaces"] == {
        surface: (
            "tests/security/test_observable_surfaces.py::"
            f"test_credential_canary_absent_from_{surface.replace('-', '_')}"
        )
        for surface in OBSERVABLE_OUTPUT_SURFACES
    }


def test_production_checkpointer_claim_binds_shadow_owner_restart_repro() -> None:
    matrix = load_matrix(PRODUCTION)

    assert matrix.claims["persistence_boundaries"]["LangGraph-checkpointer"] == (
        "tests/agent_runtime/test_thread_store.py::"
        "test_thread_state_checkpoint_owner_survives_shadow_id_and_restart"
    )


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (
            lambda matrix: matrix["claims"]["operations"].__setitem__(
                "read", "tests/security/test_missing.py::test_stale"
            ),
            "claims.operations.read",
        ),
        (
            lambda matrix: matrix["claims"]["operations"].__setitem__(
                "write", matrix["claims"]["operations"]["read"]
            ),
            "claims.operations.write",
        ),
        (
            lambda matrix: matrix["claims"]["operations"].__setitem__("unknown", "node"),
            "claims.operations.unknown",
        ),
        (
            lambda matrix: matrix["claims"]["operations"].__setitem__(
                "read", "tests/security/test_matrix_contract.py::test_fixture_behavioral_binding"
            ),
            "claims.operations.read",
        ),
    ],
)
def test_production_semantic_claim_mutations_fail_closed(tmp_path: Path, mutate, path: str) -> None:
    with pytest.raises(MatrixError) as raised:
        load_matrix(_write_production_matrix(tmp_path, mutate))
    assert raised.value.path == path


def test_fixture_models_the_exact_ticket_vocabulary() -> None:
    matrix = load_matrix(FIXTURE, fixture_mode=True)

    assert frozenset(matrix.protected_surfaces) == PROTECTED_SURFACES
    assert frozenset(matrix.operations) == OPERATIONS
    assert frozenset(matrix.persistence_boundaries) == PERSISTENCE_BOUNDARIES
    assert frozenset(matrix.attacks) == ATTACKS
    assert frozenset(matrix.observable_output_surfaces) == OBSERVABLE_OUTPUT_SURFACES
    assert frozenset(matrix.hostile_fixtures) == HOSTILE_FIXTURES
    assert frozenset({tier for case in matrix.cases for tier in case.tiers}) == TIERS


def test_fixture_behavioral_binding() -> None:
    matrix = load_matrix(FIXTURE, fixture_mode=True)
    case = next(case for case in matrix.cases if case.id == "fixture-workflow-read")

    assert case.coverage == "behavioral"
    assert case.tiers == ("pr-critical", "release-candidate")
    assert case.test_nodes == (
        "tests/security/test_matrix_contract.py::test_fixture_behavioral_binding",
    )
    assert matrix.coverage_report()["behavioral"] == (case.id,)


def test_fixture_refusal_binding() -> None:
    matrix = load_matrix(FIXTURE, fixture_mode=True)
    case = next(case for case in matrix.cases if case.id == "fixture-revoked-access-refusal")

    assert case.coverage == "absent-fail-closed"
    assert case.tiers == ("release-candidate",)
    assert case.test_nodes == (
        "tests/security/test_matrix_contract.py::test_fixture_refusal_binding",
    )
    assert (
        case.refusal_test == "tests/security/test_matrix_contract.py::test_fixture_refusal_binding"
    )
    assert matrix.coverage_report()["absent-fail-closed"] == (case.id,)


def test_absence_proofs_are_reported_separately_from_behavioral_coverage() -> None:
    report = load_matrix(FIXTURE, fixture_mode=True).coverage_report()

    assert report["behavioral"] == ("fixture-workflow-read",)
    assert report["absent-fail-closed"] == ("fixture-revoked-access-refusal",)


def test_matrix_requires_both_ticket_tiers(tmp_path: Path) -> None:
    path = _write_matrix(
        tmp_path,
        lambda matrix: matrix["cases"][0].__setitem__("tiers", ["release-candidate"]),
    )

    with pytest.raises(MatrixError) as raised:
        load_matrix(path, fixture_mode=True)

    assert raised.value.path == "cases"


def test_schema_version_must_be_the_integer_version_one(tmp_path: Path) -> None:
    path = _write_matrix(tmp_path, lambda matrix: matrix.__setitem__("schema_version", True))

    with pytest.raises(MatrixError) as raised:
        load_matrix(path, fixture_mode=True)

    assert raised.value.path == "schema_version"


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda matrix: matrix["cases"][0].__setitem__("coverage", []), "cases[0].coverage"),
        (lambda matrix: matrix["cases"][0].__setitem__("tiers", [[]]), "cases[0].tiers"),
        (lambda matrix: matrix["cases"][1].pop("refusal_test"), "cases[1].refusal_test"),
        (
            lambda matrix: matrix["cases"][1].__setitem__(
                "refusal_test", "tests/security/test_matrix_contract.py::other_refusal"
            ),
            "cases[1].refusal_test",
        ),
    ],
    ids=("coverage-list", "tier-list", "missing-refusal", "mismatched-refusal"),
)
def test_malformed_case_values_fail_with_stable_paths(tmp_path: Path, mutate, path: str) -> None:
    with pytest.raises(MatrixError) as raised:
        load_matrix(_write_matrix(tmp_path, mutate))

    assert raised.value.path == path


def test_invalid_utf8_matrix_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.json"
    path.write_bytes(b"\xff")

    with pytest.raises(MatrixError) as raised:
        load_matrix(path, fixture_mode=True)

    assert raised.value.path == "$"


def test_duplicate_raw_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-key.json"
    content = FIXTURE.read_text(encoding="utf-8").replace(
        '"schema_version": 1,', '"schema_version": 1, "schema_version": 1,', 1
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(MatrixError) as raised:
        load_matrix(path, fixture_mode=True)

    assert raised.value.path == "schema_version"


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda matrix: matrix["protected_surfaces"].pop(), "protected_surfaces"),
        (
            lambda matrix: matrix["cases"][1].__setitem__("coverage", "behavioral"),
            "cases[1].coverage",
        ),
        (lambda matrix: matrix["cases"][0].__setitem__("tiers", ["pr-critical"]), "cases[0].tiers"),
        (
            lambda matrix: matrix["cases"][1].__setitem__("id", "fixture-workflow-read"),
            "cases[1].id",
        ),
    ],
    ids=("missing-required-surface", "absence-bound-as-behavioral", "pr-only", "duplicate-id"),
)
def test_mutations_fail_with_a_stable_diagnostic_path(tmp_path: Path, mutate, path: str) -> None:
    with pytest.raises(MatrixError) as raised:
        load_matrix(_write_matrix(tmp_path, mutate))

    assert raised.value.path == path


def test_unknown_fields_and_invalid_or_duplicate_test_bindings_are_rejected(tmp_path: Path) -> None:
    unknown = _write_matrix(tmp_path, lambda matrix: matrix.__setitem__("unreviewed", True))
    with pytest.raises(MatrixError, match="^unreviewed: unknown field$"):
        load_matrix(unknown, fixture_mode=True)

    empty = _write_matrix(tmp_path, lambda matrix: matrix["cases"][0].__setitem__("test_nodes", []))
    with pytest.raises(MatrixError) as raised:
        load_matrix(empty, fixture_mode=True)
    assert raised.value.path == "cases[0].test_nodes"

    duplicate = _write_matrix(
        tmp_path,
        lambda matrix: matrix["cases"][1].__setitem__(
            "test_nodes",
            ["tests/security/test_matrix_contract.py::test_fixture_behavioral_binding"],
        ),
    )
    with pytest.raises(MatrixError) as raised:
        load_matrix(duplicate, fixture_mode=True)
    assert raised.value.path == "cases[1].test_nodes[0]"
