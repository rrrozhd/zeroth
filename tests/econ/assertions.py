"""Assertions for complete observations that cannot resolve interval gates."""

from zeroth.econ.decisioning import (
    COST_PER_OUTCOME_CHANGE_UNDETERMINED,
    SUCCESS_RATE_DROP_UNDETERMINED,
    SUCCESS_RATE_MINIMUM_UNDETERMINED,
)


def assert_interval_abstention(report):
    payload = report if isinstance(report, dict) else report.model_dump()
    assert payload["verdict"] == "abstain"
    assert payload["recommended_action"] == "collect_evidence"
    assert payload["reason_codes"]
    assert set(payload["reason_codes"]) <= {
        COST_PER_OUTCOME_CHANGE_UNDETERMINED,
        SUCCESS_RATE_DROP_UNDETERMINED,
        SUCCESS_RATE_MINIMUM_UNDETERMINED,
    }
    assert payload["success_rate_change_interval"] is not None
    assert payload["candidate_success_rate_interval"] is not None
