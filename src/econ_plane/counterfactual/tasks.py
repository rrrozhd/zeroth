import dramatiq

from econ_plane.common.worker import redis_broker  # noqa: F401
from econ_plane.counterfactual.schemas import EvaluationRunRequest
from econ_plane.counterfactual.service import run_evaluation
from econ_plane.database import SessionLocal


@dramatiq.actor
def run_evaluation_async(payload: dict) -> None:
    request = EvaluationRunRequest(**payload)
    with SessionLocal() as db:
        run_evaluation(db, request)
