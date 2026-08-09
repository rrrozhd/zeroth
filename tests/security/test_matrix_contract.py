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


def _write_matrix(tmp_path: Path, mutate) -> Path:
    matrix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutate(matrix)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    return path


def test_fixture_models_the_exact_ticket_vocabulary() -> None:
    matrix = load_matrix(FIXTURE)

    assert frozenset(matrix.protected_surfaces) == PROTECTED_SURFACES
    assert frozenset(matrix.operations) == OPERATIONS
    assert frozenset(matrix.persistence_boundaries) == PERSISTENCE_BOUNDARIES
    assert frozenset(matrix.attacks) == ATTACKS
    assert frozenset(matrix.observable_output_surfaces) == OBSERVABLE_OUTPUT_SURFACES
    assert frozenset(matrix.hostile_fixtures) == HOSTILE_FIXTURES
    assert frozenset({tier for case in matrix.cases for tier in case.tiers}) == TIERS


def test_fixture_behavioral_binding() -> None:
    assert True


def test_fixture_refusal_binding() -> None:
    assert True


def test_absence_proofs_are_reported_separately_from_behavioral_coverage() -> None:
    report = load_matrix(FIXTURE).coverage_report()

    assert report["behavioral"] == ("fixture-workflow-read",)
    assert report["absent-fail-closed"] == ("fixture-revoked-access-refusal",)


def test_matrix_requires_both_ticket_tiers(tmp_path: Path) -> None:
    path = _write_matrix(
        tmp_path,
        lambda matrix: matrix["cases"][0].__setitem__("tiers", ["release-candidate"]),
    )

    with pytest.raises(MatrixError) as raised:
        load_matrix(path)

    assert raised.value.path == "cases"


def test_schema_version_must_be_the_integer_version_one(tmp_path: Path) -> None:
    path = _write_matrix(tmp_path, lambda matrix: matrix.__setitem__("schema_version", True))

    with pytest.raises(MatrixError) as raised:
        load_matrix(path)

    assert raised.value.path == "schema_version"


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda matrix: matrix["protected_surfaces"].pop(), "protected_surfaces"),
        (lambda matrix: matrix["cases"][1].__setitem__("coverage", "behavioral"), "cases[1].coverage"),
        (lambda matrix: matrix["cases"][0].__setitem__("tiers", ["pr-critical"]), "cases[0].tiers"),
        (lambda matrix: matrix["cases"][1].__setitem__("id", "fixture-workflow-read"), "cases[1].id"),
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
        load_matrix(unknown)

    empty = _write_matrix(tmp_path, lambda matrix: matrix["cases"][0].__setitem__("test_nodes", []))
    with pytest.raises(MatrixError) as raised:
        load_matrix(empty)
    assert raised.value.path == "cases[0].test_nodes"

    duplicate = _write_matrix(
        tmp_path,
        lambda matrix: matrix["cases"][1].__setitem__(
            "test_nodes", ["tests/security/test_matrix_contract.py::test_fixture_behavioral_binding"]
        ),
    )
    with pytest.raises(MatrixError) as raised:
        load_matrix(duplicate)
    assert raised.value.path == "cases[1].test_nodes[0]"
