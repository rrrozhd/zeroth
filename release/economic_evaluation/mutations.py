"""Isolated in-memory candidate faults; no worktree or oracle modifications."""

import inspect
import io
import math
import unittest
from unittest.mock import patch

from zeroth.econ import probabilistic as candidate

PENDING = []


def run_case(test_name):
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName(test_name)
    )
    return result, output.getvalue()


def run():
    base = "release.economic_evaluation.test_invariants.CandidateInvariantTests."
    source = inspect.getsource(candidate._diagnose_model_migration)
    definitions = [
        (
            "omit_critical_error_penalties",
            "float(policy.critical_error_penalty_usd) * incremental_critical_errors",
            "0.0",
            "release.economic_evaluation.test_finite_adapters.FrozenCandidateAdapterTests."
            "test_k10_signed_penalized_loss_keeps_savings_separate",
        ),
        (
            "choose_infeasible_action",
            "action.feasible and action.expected_monthly_savings_usd > 0",
            "action.expected_monthly_savings_usd > 0",
            "release.economic_evaluation.test_finite_adapters.FrozenCandidateAdapterTests."
            "test_k07_never_select_infeasible_cheaper_action",
        ),
        (
            "treat_missing_as_measured",
            '"critical_error" not in row.model_fields_set',
            "False",
            "release.economic_evaluation.test_forecast_defects.ForecastDefectTests."
            "test_missing_critical_measurement_is_not_measured_false",
        ),
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
            candidate, "_diagnose_model_migration", namespace["_diagnose_model_migration"]
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

    pairing_source = inspect.getsource(candidate._paired_observations)
    before = "[candidate_by_id[case_id] for case_id in paired_ids]"
    if before not in pairing_source:
        results.append({"fault": "break_pairing_cohorts", "status": "injection_failed"})
    else:
        namespace = dict(candidate.__dict__)
        exec(
            compile(
                pairing_source.replace(
                    before, "[candidate_by_id[paired_ids[0]] for case_id in paired_ids]"
                ),
                "<fault:break-pairing>",
                "exec",
            ),
            namespace,
        )
        test = (
            "release.economic_evaluation.test_forecast_defects.ForecastDefectTests."
            "test_identical_paired_heterogeneous_outcomes_have_zero_incremental_loss"
        )
        baseline, baseline_output = run_case(test)
        with patch.object(candidate, "_paired_observations", namespace["_paired_observations"]):
            result, output = run_case(test)
        results.append(
            {
                "fault": "break_pairing_cohorts",
                "status": "detected"
                if baseline.wasSuccessful() and result.failures and not result.errors
                else "survived_or_error",
                "test": test,
                "baseline_passed": baseline.wasSuccessful(),
                "baseline_output": baseline_output,
                "fault_output": output,
            }
        )

    from zeroth.econ.plane.reports import pdf, service

    boundary_specs = [
        (
            "substitute_report_action",
            pdf,
            "render_decision_report_pdf",
            '    share = _percent(report.get("recommended_candidate_share", 0))',
            "    selected = actions[-1]\n"
            '    share = _percent(report.get("recommended_candidate_share", 0))',
            "test_report_tree_uses_selected_fixture_not_substituted_action",
        ),
        (
            "bypass_tenant_ownership",
            service,
            "_require_scope",
            "if type(db) is not ScopedSession or db.scope is None:",
            "if False:",
            "test_unscoped_report_access_cannot_bypass_tenant_ownership",
        ),
    ]
    for name, module, function_name, before, after, method in boundary_specs:
        source_text = inspect.getsource(getattr(module, function_name))
        test = (
            "release.economic_evaluation.test_named_boundary_faults.NamedBoundaryFaultTests."
            + method
        )
        baseline, baseline_output = run_case(test)
        if before not in source_text:
            results.append({"fault": name, "status": "injection_failed"})
            continue
        namespace = dict(module.__dict__)
        exec(compile(source_text.replace(before, after), f"<fault:{name}>", "exec"), namespace)
        with patch.object(module, function_name, namespace[function_name]):
            result, output = run_case(test)
        results.append(
            {
                "fault": name,
                "status": "detected"
                if baseline.wasSuccessful() and result.failures and not result.errors
                else "survived_or_error",
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
