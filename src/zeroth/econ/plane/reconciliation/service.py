from __future__ import annotations

from zeroth.econ.plane.costing.models import CalibrationMetric, GroundTruthCost
from zeroth.econ.plane.costing.service import (
    add_ground_truth_rows as _add_ground_truth_rows,
    compute_calibration_summary as _compute_calibration_summary,
)
from zeroth.econ.plane.scoped_session import ScopedSession

# Keep the historical public annotation stable while the operational API uses
# the explicitly scoped implementation from ``costing.service``.
Session = ScopedSession


def add_ground_truth_rows(db: Session, rows: list[GroundTruthCost]) -> int:
    return _add_ground_truth_rows(db, rows)


def compute_calibration_summary(db: Session) -> list[CalibrationMetric]:
    return _compute_calibration_summary(db)

__all__ = ["add_ground_truth_rows", "compute_calibration_summary"]
