"""Exact original costs must remain exact at the existing admission boundary."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import (
    engine as database_engine,
    execution,
    scoped,
    user,
)
from zeroth.econ.plane.cloud.api import record_execution
from zeroth.econ.plane.enforcement.models import TenantBudget
from zeroth.econ.plane.enforcement.service import (
    CostReservationDenied,
    get_budget_status,
    reserve_cost,
)
from zeroth.econ.plane.performance import models as performance_models  # noqa: F401

engine = database_engine


def seed(engine, amounts):
    with Session(engine) as raw:
        raw.add(
            TenantBudget(tenant_id="tenant-a", budget_cap_usd=0.4, updated_at=datetime.now(UTC))
        )
        raw.commit()
        db = scoped(raw)
        for i, amount in enumerate(amounts):
            record_execution(
                execution(
                    event_id=f"charge-{i}",
                    cost_role="charge",
                    charge_id=f"charge-{i}",
                    cost_usd=amount,
                    recorded_at=datetime.now(UTC),
                ),
                db,
                user(),
            )


@pytest.mark.parametrize("amounts", [[".1", ".2"], [".3"], [".01"] * 30])
def test_exact_execution_spend_admits_at_cap_and_rejects_one_unit_over(engine, amounts):
    seed(engine, amounts)
    with Session(engine) as raw:
        db = scoped(raw)
        with pytest.raises(CostReservationDenied, match="tenant ceiling"):
            reserve_cost(db, operation_id="over", max_cost_usd=Decimal(".10000001"))
        raw.rollback()
        admitted = reserve_cost(db, operation_id="at-cap", max_cost_usd=Decimal(".1"))
        assert admitted.operation_id == "at-cap"


@pytest.mark.parametrize("amounts", [[".1", ".2"], [".3"], [".01"] * 30])
def test_budget_status_is_invariant_to_splitting_equal_spend(engine, amounts):
    seed(engine, amounts)
    with Session(engine) as raw:
        status = get_budget_status(scoped(raw), "tenant-a")
    # The existing budget response is a float. Sum exact inputs first, then
    # convert once at that response boundary; do not expose binary SQL residue.
    assert status["paid_spend_usd"] == float(Decimal(".3"))
