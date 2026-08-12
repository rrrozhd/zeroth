from __future__ import annotations

from zeroth.econ.plane.costing.models import CalibrationMetric, GroundTruthCost
from zeroth.econ.plane.costing.service import (
    add_ground_truth_rows as _add_ground_truth_rows,
    compute_calibration_summary as _compute_calibration_summary,
)
from zeroth.econ.plane.scoped_session import ScopedSession

# This alias preserves the immutable annotation spelling only. Runtime access
# is authorized independently by the exact-type boundary below.
Session = ScopedSession


def _require_exact_scoped_session(db: object) -> ScopedSession:
    if type(db) is not ScopedSession:
        raise TypeError("reconciliation persistence requires an exact ScopedSession")
    return db


def add_ground_truth_rows(db: Session, rows: list[GroundTruthCost]) -> int:
    return _add_ground_truth_rows(_require_exact_scoped_session(db), rows)


def compute_calibration_summary(db: Session) -> list[CalibrationMetric]:
    return _compute_calibration_summary(_require_exact_scoped_session(db))

__all__ = ["add_ground_truth_rows", "compute_calibration_summary"]
