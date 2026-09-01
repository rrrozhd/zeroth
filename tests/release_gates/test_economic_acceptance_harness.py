from __future__ import annotations

from decimal import Decimal

import pytest


def test_seed_contract_closes_provider_truth_to_two_resolved_outcomes() -> None:
    from release.economic_acceptance import seed_contract

    contract = seed_contract()

    assert contract["outcome_definition"]["target"] is True
    assert len(contract["executions"]) == 3
    assert {item["run_id"] for item in contract["executions"]} == {
        "run-success",
        "run-failed",
    }
    assert {item["outcome_value"] for item in contract["outcomes"]} == {True, False}
    assert sum(Decimal(item["token_cost_usd"]) for item in contract["executions"]) == Decimal(
        contract["provider_statement"]["billed_total_usd"]
    )
    assert all(
        item["metadata"]["provider"] == "openai" for item in contract["executions"]
    )


def test_report_binds_candidate_headless_install_and_claim_limited_artifacts(
    tmp_path, candidate
) -> None:
    from gates.identity import identity_digest
    from release.economic_acceptance import build_report

    diagnostic_markdown = tmp_path / "diagnostic.md"
    diagnostic_markdown.write_text(
        "Failed-run exposure identifies where money accumulated, not which step caused the failure.\n"
        "This report observes production history; it does not prove savings.\n",
        encoding="utf-8",
    )
    reconciliation_markdown = tmp_path / "reconciliation.md"
    reconciliation_markdown.write_text(
        "# Provider bill closure\n**Reconciliation state:** reconciled\n",
        encoding="utf-8",
    )

    report = build_report(
        candidate=candidate,
        artifact_digest=next(iter(candidate["package"]["artifacts"].values())),
        installed_distributions={"zeroth-core": candidate["package"]["version"]},
        diagnostic={
            "claim_scope": "observed_economic_exposure",
            "decision_state": "economic_risk_observed",
            "measured_failure_exposure_usd": 0.4,
        },
        diagnostic_markdown=diagnostic_markdown,
        reconciliation={
            "reconciliation_state": "reconciled",
            "unreconciled_billed_usd": "0.00",
            "outcome_unresolved_usd": "0.00",
        },
        reconciliation_markdown=reconciliation_markdown,
    )

    assert report["candidate_digest"] == identity_digest(candidate)
    assert report["excluded_distributions"] == {
        "zeroth-console": "absent",
        "zeroth-sdk": "absent",
    }
    assert report["diagnostic"]["markdown_sha256"].startswith("sha256:")
    assert report["reconciliation"]["markdown_sha256"].startswith("sha256:")
    assert report["status"] == "passed"


@pytest.mark.parametrize(
    ("installed", "reconciliation", "message"),
    [
        (
            {"zeroth-core": "0.19", "zeroth-console": "0.19"},
            {"reconciliation_state": "reconciled", "unreconciled_billed_usd": "0"},
            "headless",
        ),
        (
            {"zeroth-core": "0.19"},
            {"reconciliation_state": "unreconciled", "unreconciled_billed_usd": "0.10"},
            "reconciliation",
        ),
    ],
)
def test_report_refuses_non_headless_or_unclosed_evidence(
    tmp_path, candidate, installed, reconciliation, message
) -> None:
    from release.economic_acceptance import build_report

    diagnostic_markdown = tmp_path / "diagnostic.md"
    diagnostic_markdown.write_text("not which step caused the failure\ndoes not prove savings\n")
    reconciliation_markdown = tmp_path / "reconciliation.md"
    reconciliation_markdown.write_text("reconciled\n")

    with pytest.raises(ValueError, match=message):
        build_report(
            candidate=candidate,
            artifact_digest=next(iter(candidate["package"]["artifacts"].values())),
            installed_distributions=installed,
            diagnostic={
                "claim_scope": "observed_economic_exposure",
                "decision_state": "economic_risk_observed",
            },
            diagnostic_markdown=diagnostic_markdown,
            reconciliation=reconciliation,
            reconciliation_markdown=reconciliation_markdown,
        )
