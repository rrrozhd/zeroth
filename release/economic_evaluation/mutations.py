"""Isolated in-memory candidate faults; no worktree or oracle modifications."""

import inspect
import io
import math
import unittest
from unittest.mock import patch

from zeroth.econ import probabilistic as candidate

PENDING = [
    "omit_critical_error_penalties",
    "break_pairing_cohorts",
    "choose_infeasible_action",
    "treat_missing_as_measured",
    "substitute_report_action",
    "bypass_tenant_ownership",
]


def run_case(test_name):
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName(test_name)
    )
    return result, output.getvalue()


def run():
    base = "release.economic_evaluation.test_invariants.CandidateInvariantTests."
    source = inspect.getsource(candidate.recommend_model_migration)
    definitions = [
        (
            "reverse_savings_sign",
            'values["savings"].append(baseline_cost - action_cost)',
            'values["savings"].append(action_cost - baseline_cost)',
            base + "test_k02_and_model_swap",
        ),
        (
            "swap_model_labels",
            "incumbent_model=evidence.incumbent_model,",
            "incumbent_model=evidence.candidate_model,",
            base + "test_k02_and_model_swap",
        ),
        (
            "bypass_evidence_readiness",
            'policy.require_calibrated_forecast and readiness.calibration_state != "calibrated"',
            "False",
            base + "test_missing_calibration_abstains_without_forecasts",
        ),
    ]
    results = []
    for name, before, after, test in definitions:
        baseline, baseline_output = run_case(test)
        if before not in source:
            results.append({"fault": name, "status": "injection_failed", "target": before})
            continue
        namespace = dict(candidate.__dict__)
        exec(compile(source.replace(before, after), f"<fault:{name}>", "exec"), namespace)
        with patch.object(
            candidate, "recommend_model_migration", namespace["recommend_model_migration"]
        ):
            result, output = run_case(test)
        detected = baseline.wasSuccessful() and bool(result.failures) and not result.errors
        results.append(
            {
                "fault": name,
                "status": "detected" if detected else "survived_or_error",
                "test": test,
                "baseline_passed": baseline.wasSuccessful(),
                "baseline_output": baseline_output,
                "fault_output": output,
            }
        )

    def rounded_tail(losses, *, confidence):
        ordered = sorted(losses)
        tail = ordered[-max(1, math.ceil((1 - confidence) * len(ordered))) :]
        return ordered[math.ceil(confidence * len(ordered)) - 1], sum(tail) / len(tail)

    test = (
        "release.economic_evaluation.test_known_answers.CandidateTailTests."
        "test_k05_fractional_worst_probability_mass"
    )
    baseline, baseline_output = run_case(test)
    with patch.object(candidate, "empirical_var_cvar", rounded_tail):
        result, output = run_case(test)
    results.append(
        {
            "fault": "round_cvar_tail_to_whole_rows",
            "status": "detected"
            if baseline.wasSuccessful() and result.failures and not result.errors
            else "survived_or_error",
            "test": test,
            "baseline_passed": baseline.wasSuccessful(),
            "baseline_output": baseline_output,
            "fault_output": output,
        }
    )
    results.extend({"fault": name, "status": "not_implemented"} for name in PENDING)
    return {
        "required_detection": "10/10 named faults",
        "passed": all(row["status"] == "detected" for row in results),
        "detected": sum(row["status"] == "detected" for row in results),
        "survivors_or_injection_errors": [
            row["fault"] for row in results if row["status"] not in {"detected", "not_implemented"}
        ],
        "unexecuted": PENDING,
        "results": results,
    }
