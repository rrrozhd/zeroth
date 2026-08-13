"""``latest_cost_estimate`` returns one row (ZER-48 / A01-5).

The query ordered by id descending and then called ``scalar_one_or_none``, with
no ``.limit(1)``.  Ordering does not reduce a result set, so the *second* cost
estimate recorded for a capability turned every subsequent read into
``sqlalchemy.exc.MultipleResultsFound`` — a failure on the second estimation run,
not on some rare edge.  The sibling services (counterfactual, performance,
capabilities) all carry the bound this one was missing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeroth.econ.plane.costing.models import CostEstimate
from zeroth.econ.plane.costing.service import latest_cost_estimate
from zeroth.econ.plane.database import Base
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import TenantWideScopeContext


@pytest.fixture
def session():  # noqa: ANN201
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as inner:
        yield ScopedSession(inner, TenantWideScopeContext.for_default_compatibility())


_NOW = datetime(2026, 8, 12, tzinfo=UTC)


def _estimate(capability_id: str, cost: float) -> CostEstimate:
    return CostEstimate(
        tenant_id="default",
        capability_id=capability_id,
        period_start=_NOW,
        period_end=_NOW,
        total_cost_estimate_usd=cost,
        data_quality="measured",
    )


def test_one_estimate_is_returned(session: ScopedSession) -> None:
    session.add(_estimate("cap1", 1.0))
    session.commit()

    assert latest_cost_estimate(session, "cap1") is not None


def test_a_second_estimate_does_not_break_the_read(session: ScopedSession) -> None:
    """This is the exact reproduction: two rows for one capability."""
    session.add(_estimate("cap1", 1.0))
    session.add(_estimate("cap1", 2.0))
    session.commit()

    latest = latest_cost_estimate(session, "cap1")

    assert latest is not None
    assert float(latest.total_cost_estimate_usd) == 2.0, "the newest estimate must win"


def test_missing_capability_is_still_none(session: ScopedSession) -> None:
    assert latest_cost_estimate(session, "absent") is None
