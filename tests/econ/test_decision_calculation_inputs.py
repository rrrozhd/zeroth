"""Reconstruct retained numbers from portable inputs without production aggregators."""

from copy import deepcopy
from decimal import Decimal
from fractions import Fraction

import pytest

from zeroth.econ.decisioning import (
    DecisionPolicy, EconomicDecision, RunEvidence, VersionEvidence, compare_workflow_versions,
)


def reference_economics(rows, *, allow_estimated_cost):
    """Independent counting and rational-money reference for run-economics/1."""
    counts = dict.fromkeys(("runs", "labeled_runs", "accepted_runs", "rejected_runs",
                           "inferred_outcome_runs", "measured_runs", "estimated_runs",
                           "unmeasured_runs"), 0)
    totals = {"measured": Fraction(0), "estimated": Fraction(0)}
    for row in rows:
        n = row["runs"]
        counts["runs"] += n
        counts[f"{row['cost_measurement']}_runs"] += n
        if row["cost_usd"] is not None:
            totals[row["cost_measurement"]] += n * Fraction(row["cost_usd"])
        if row["accepted"] is not None:
            counts["labeled_runs"] += n
            counts["accepted_runs" if row["accepted"] else "rejected_runs"] += n
            if row["outcome_measurement"] != "measured":
                counts["inferred_outcome_runs"] += n

    def decimal(value):
        return Decimal(value.numerator) / value.denominator

    available = (
        counts["labeled_runs"] == counts["runs"] and not counts["unmeasured_runs"]
        and (allow_estimated_cost or not counts["estimated_runs"])
        and counts["accepted_runs"] > 0
    )
    total = totals["measured"] + (totals["estimated"] if allow_estimated_cost else 0)
    return {
        **counts,
        "measured_cost_usd": decimal(totals["measured"]),
        "estimated_cost_usd": decimal(totals["estimated"]),
        "outcome_coverage": counts["labeled_runs"] / counts["runs"] if counts["runs"] else 0.0,
        "success_rate": counts["accepted_runs"] / counts["labeled_runs"] if counts["labeled_runs"] else None,
        "cost_per_accepted_outcome_usd": decimal(total / counts["accepted_runs"]) if available else None,
    }


@pytest.mark.parametrize("allow_estimated", [False, True])
@pytest.mark.parametrize("case", ["mixed", "complete", "no_success", "empty"])
def test_portable_calculation_inputs_reconstruct_every_version_number(case, allow_estimated):
    # This source ledger predates calculation output; IDs must not enter the artifact.
    ledger = [
        ("1.10", "measured", True, "measured"),
        ("1.1000", "measured", True, "measured"),
        ("0", "measured", False, "measured"),
        ("0.00000001", "estimated", True, "estimated"),
    ]
    if case == "mixed":
        ledger += [(None, "unmeasured", None, "unmeasured")]
    if case == "no_success":
        ledger = [(amount, state, False, provenance) for amount, state, _, provenance in ledger]
    if case == "empty":
        ledger = []
    runs = [RunEvidence(run_id=f"private-source-run-{i}", cost_usd=amount,
                        cost_measurement=state, accepted=accepted, outcome_measurement=provenance)
            for i, (amount, state, accepted, provenance) in enumerate(ledger)]
    policy = DecisionPolicy(min_runs=1, min_success_rate=0.5, allow_estimated_cost=allow_estimated)

    def compare(items):
        return compare_workflow_versions(
            VersionEvidence(workflow="invoice", version="v1", runs=items),
            VersionEvidence(workflow="invoice", version="v2", runs=items), policy=policy,
        )

    report = compare(runs)
    portable = report.model_dump(mode="json").get("calculation_inputs")
    assert portable is not None, "retained results need their original calculation inputs"
    assert portable["version"] == "run-economics/1"
    for side in ("baseline", "candidate"):
        expected = reference_economics(portable[side], allow_estimated_cost=allow_estimated)
        actual = getattr(report, side).model_dump(exclude={"workflow", "version"})
        assert actual == expected
        assert sum(row["runs"] for row in portable[side]) == len(ledger)
        assert all(set(row) == {"cost_usd", "cost_measurement", "accepted", "outcome_measurement", "runs"}
                   for row in portable[side])
    assert "private-source-run-" not in report.model_dump_json()
    assert compare(list(reversed(runs))).model_dump(mode="json")["calculation_inputs"] == portable
    if case in {"mixed", "complete"}:
        assert report.baseline.measured_cost_usd == Decimal("2.20")
        assert report.baseline.estimated_cost_usd == Decimal("0.00000001")
        assert report.baseline.accepted_runs == 3
        assert len(portable["baseline"]) == len(ledger) - 1
        grouped = next(row for row in portable["baseline"] if row["cost_usd"] == "1.1")
        assert grouped["runs"] == 2
    # History round-trips include the exact portable numbers; old history stays absent.
    assert EconomicDecision.model_validate_json(report.model_dump_json()) == report
    legacy = deepcopy(report.model_dump(mode="json"))
    legacy.pop("calculation_inputs")
    assert EconomicDecision.model_validate(legacy).calculation_inputs is None
