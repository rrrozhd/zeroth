"""Frozen calculation history survives changes to live source evidence."""

import json
from tests.econ.assertions import assert_interval_abstention

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.econ.test_decision_calculation_inputs import reference_economics
from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW, definition, engine as database_engine, execution, outcome, scoped, user,
)
from tests.econ_plane.test_source_inventory import reference_digest
from zeroth.econ.charge_costs import ChargeCostRevision
from zeroth.econ.plane.cloud.api import record_execution, record_outcome
from zeroth.econ.plane.decisioning.schemas import VersionComparisonRequest
from zeroth.econ.plane.decisioning.service import (
    compare_versions_from_store, list_retained_decisions, retain_decision,
)
from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser
from zeroth.econ.plane.instrumentation.charge_costs import ingest_revision

engine = database_engine


@pytest.mark.parametrize("reverse", [False, True])
def test_retained_calculations_reconstruct_after_late_evidence_and_source_erasure(engine, reverse):
    # Independent ledger and membership are fixed before delivery. The second
    # charge for run-a is a physical retry, not another workflow run.
    ledger = {
        "v1": [("private-run-a", "first", "1"), ("private-run-a", "retry", ".2"),
               ("private-run-b", "first", ".8")],
        "v2": [("private-run-a", "first", ".6"), ("private-run-a", "retry", ".1"),
               ("private-run-b", "first", ".7")],
    }
    windows = {}
    for side, version in [("baseline", "v1"), ("candidate", "v2")]:
        windows[side] = {
            "source_window_id": "frozen-ledger", "opened_at": NOW,
            "closed_at": NOW + timedelta(hours=1),
            "runs": [dict(run_id=run, terminal_state="completed", execution_count=len(ids),
                          execution_ids_digest=reference_digest(ids))
                     for run in ("private-run-a", "private-run-b")
                     for ids in [[f"{version}-{r}-{step}" for r, step, _ in ledger[version] if r == run]]],
        }
    request = VersionComparisonRequest(
        workflow="invoice", baseline_version="v1", candidate_version="v2",
        source_windows=windows, policy={"min_runs": 1, "min_success_rate": .5},
    )
    for tenant in ("tenant-a", "tenant-b"):
        with Session(engine) as raw:
            db, who = scoped(raw, tenant), user(tenant)
            for version, rows in ledger.items():
                definition(db, "invoice", version)
                for run, step, amount in reversed(rows) if reverse else rows:
                    event = execution(
                        version=version, run=run, event_id=f"{version}-{run}-{step}",
                        cost_role="charge", charge_id=f"{version}-{run}-{step}",
                        source_window_id="frozen-ledger", step=step,
                        cost_usd=amount, attempt=2 if step == "retry" else 1,
                        metadata={"prompt": "private-payload-marker"},
                    )
                    assert record_execution(event, db, who).status == "inserted"
                    assert record_execution(event, db, who).status == "duplicate"
                for run in ("private-run-a", "private-run-b"):
                    record_outcome(outcome(version=version, run=run), db, who)

    def compare(tenant="tenant-a"):
        with Session(engine) as raw:
            db = scoped(raw, tenant)
            return retain_decision(db, request, compare_versions_from_store(db, request), evaluated_by="test")

    first, foreign = compare(), compare("tenant-b")
    assert_interval_abstention(first)
    assert first.baseline.measured_cost_usd == Decimal("2")
    assert first.candidate.measured_cost_usd == Decimal("1.4")
    assert first.candidate.cost_per_accepted_outcome_usd == Decimal(".7")
    assert first.decision_id != foreign.decision_id
    assert compare().decision_id == first.decision_id
    assert first.calculation_inputs is not None
    assert "private-run-" not in first.model_dump_json()
    assert "private-payload-marker" not in first.model_dump_json()
    assert len(first.calculation_inputs.candidate) == 1
    assert first.calculation_inputs.candidate[0].runs == 2

    with Session(engine) as raw:
        ingest_revision(scoped(raw), ChargeCostRevision(
            charge_id="v2-private-run-a-first", asserted_at=NOW + timedelta(hours=2),
            token_cost_usd="2.6", cost_measurement="measured", reason="statement correction",
        ))
    corrected = compare()
    assert_interval_abstention(corrected)
    assert corrected.cost_per_outcome_change > 0
    assert corrected.candidate.measured_cost_usd == Decimal("3.4")
    assert corrected.decision_id != first.decision_id
    with Session(engine) as raw:
        db = scoped(raw)
        # A newer withdrawal suppresses success; its older delayed assertion
        # arrives afterward and must not restore the old outcome.
        record_outcome(outcome(version="v2", run="private-run-b", accepted=None,
                               maturity="withdrawn", occurred_at=NOW + timedelta(hours=3)), db, user())
        record_outcome(outcome(version="v2", run="private-run-b", accepted=False,
                               occurred_at=NOW + timedelta(hours=2)), db, user())
    withdrawn = compare()
    assert withdrawn.verdict == "abstain"
    assert withdrawn.candidate.labeled_runs == 1
    assert withdrawn.candidate.cost_per_accepted_outcome_usd is None
    eraser = SqlAlchemyEconEventEraser(sessionmaker(bind=engine))
    assert eraser._delete_sync("tenant-a", ["private-run-a", "private-run-b"], "erase-frozen-ledger") > 0
    empty = compare()
    assert empty.verdict == "abstain" and empty.candidate.unmeasured_runs == 2
    with Session(engine) as raw:
        history = {report.decision_id: report for report in list_retained_decisions(scoped(raw))}
        assert foreign.decision_id not in history
    with Session(engine) as raw:
        foreign_history = list_retained_decisions(scoped(raw, "tenant-b"))
        assert foreign_history == [foreign]
    for original in (first, corrected, withdrawn, empty):
        stored = history[original.decision_id]
        assert stored == original
        # Independent reference consumes the exported JSON, never live ORM rows
        # or the production summary helper. Every headline count/money is covered.
        portable = json.loads(stored.model_dump_json())["calculation_inputs"]
        for side in ("baseline", "candidate"):
            assert getattr(stored, side).model_dump(exclude={"workflow", "version"}) == reference_economics(
                portable[side], allow_estimated_cost=stored.policy.allow_estimated_cost,
            )
