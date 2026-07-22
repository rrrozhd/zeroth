import dramatiq

from zeroth.econ.plane.common.worker import redis_broker  # noqa: F401
from zeroth.econ.plane.counterfactual.schemas import EvaluationRunRequest
from zeroth.econ.plane.counterfactual.service import run_evaluation
from zeroth.econ.plane.database import SessionLocal


@dramatiq.actor
def run_evaluation_async(payload: dict) -> None:
    request = EvaluationRunRequest(**payload)
    with SessionLocal() as db:
        run_evaluation(db, request)
