from __future__ import annotations

import json

from zeroth.econ.plane.reconciliation.schemas import ProviderBillImportRequest
from zeroth.service import economic_diagnostic_cli as diagnostic_cli


_REPORT = {
    "workflow_id": "invoice-processing",
    "window_start": None,
    "window_end": None,
    "cohort_dimension": "plan",
    "claim_scope": "observed_economic_exposure",
    "decision_state": "economic_risk_observed",
    "data_quality": "measured_only",
    "event_count": 3,
    "runs": 2,
    "successful_runs": 1,
    "failed_runs": 1,
    "unresolved_runs": 0,
    "undefined_outcome_versions": [],
    "outcome_coverage": 1.0,
    "measured_events": 3,
    "estimated_events": 0,
    "unmeasured_events": 0,
    "incomplete_events": 0,
    "measured_cost_usd": 0.5,
    "estimated_cost_usd": 0.0,
    "measured_failure_exposure_usd": 0.4,
    "estimated_failure_exposure_usd": 0.0,
    "measured_cost_per_successful_outcome_usd": 0.5,
    "estimated_cost_per_successful_outcome_usd": 0.0,
    "top_failure_exposure": {
        "workflow_id": "invoice-processing",
        "workflow_version": "v1",
        "step_id": "extract",
        "failed_runs": 1,
        "measured_failure_exposure_usd": 0.4,
        "estimated_failure_exposure_usd": 0.0,
        "measured_repeated_attempt_cost_usd": 0.1,
        "estimated_repeated_attempt_cost_usd": 0.0,
        "attribution": "failed_run_exposure_not_step_causality",
    },
    "highest_failure_rate_cohort": {
        "cohort": "free",
        "runs": 1,
        "successful_runs": 0,
        "failed_runs": 1,
        "measured_cost_usd": 0.4,
        "estimated_cost_usd": 0.0,
        "measured_cost_per_successful_outcome_usd": None,
        "estimated_cost_per_successful_outcome_usd": None,
        "incomplete_events": 0,
    },
    "recommended_action": {
        "code": "investigate_retry_policy",
        "rationale": "$0.10000000 measured cost came from explicit repeated attempts in failed runs.",
        "supported_claim": "Repeated-attempt cost is observed; changing retries is unproven.",
    },
    "limitations": [
        "Failed-run exposure identifies where money accumulated, not which step caused the failure."
    ],
}

_BILL_REPORT = {
    "statement_id": "openai-2026-08",
    "provider": "openai",
    "statement_digest": "sha256:abc",
    "period_start": "2026-08-01T00:00:00Z",
    "period_end": "2026-09-01T00:00:00Z",
    "currency": "USD",
    "reconciliation_state": "allocated_with_variance",
    "billed_total_usd": "0.60",
    "allocated_billed_usd": "0.60",
    "unreconciled_billed_usd": "0",
    "telemetry_measured_usd": "0.50",
    "telemetry_variance_usd": "0.10",
    "unbilled_telemetry_usd": "0",
    "outcome_unresolved_usd": "0",
    "matched_buckets": 1,
    "unmatched_buckets": [],
    "allocations": [
        {
            "bucket_id": "project-a",
            "model": None,
            "provider_dimensions": {"project_id": "proj_a"},
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "outcome_status": "success",
            "billed_cost_usd": "0.36",
            "telemetry_cost_usd": "0.30",
            "run_count": 1,
            "event_count": 1,
        }
    ],
    "allocation_method": "measured_cost_proportional",
    "limitations": ["Estimated telemetry never becomes provider truth."],
}

_OPENAI_COSTS_PAGE = {
    "object": "page",
    "data": [
        {
            "object": "bucket",
            "start_time": 1785542400,
            "end_time": 1785628800,
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.06, "currency": "usd"},
                    "line_item": "Text models",
                    "project_id": "proj_a",
                },
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.04, "currency": "usd"},
                    "line_item": "Web search",
                    "project_id": "proj_a",
                },
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.25, "currency": "usd"},
                    "line_item": "Text models",
                    "project_id": "proj_b",
                },
            ],
        },
        {
            "object": "bucket",
            "start_time": 1785628800,
            "end_time": 1785715200,
            "results": [
                {
                    "object": "organization.costs.result",
                    "amount": {"value": 0.15, "currency": "usd"},
                    "line_item": "Text models",
                    "project_id": "proj_a",
                }
            ],
        },
    ],
    "has_more": False,
    "next_page": None,
}


def test_markdown_report_leads_with_the_decision_and_preserves_claim_limits() -> None:
    rendered = diagnostic_cli.render_markdown(_REPORT)

    assert rendered.startswith("# Zeroth economic diagnostic: invoice-processing\n")
    assert "**Decision state:** economic risk observed" in rendered
    assert "| Measured failed-run exposure | $0.40000000 |" in rendered
    assert "extract" in rendered
    assert "free" in rendered
    assert "changing retries is unproven" in rendered
    assert "not which step caused the failure" in rendered
    assert "savings opportunity" not in rendered.lower()


def test_markdown_report_names_versions_without_success_semantics() -> None:
    report = {
        **_REPORT,
        "decision_state": "insufficient_evidence",
        "undefined_outcome_versions": ["v2", "v3"],
        "recommended_action": {
            "code": "define_outcome_success",
            "rationale": "2 workflow version(s) have no immutable outcome definition.",
            "supported_claim": "Cost is observed, but success remains undefined.",
        },
    }

    rendered = diagnostic_cli.render_markdown(report)

    assert "**Undefined outcome versions:** `v2`, `v3`" in rendered
    assert "define outcome success" in rendered


def test_diagnose_fetches_with_an_environment_token_and_prints_json(
    monkeypatch, capsys
) -> None:
    observed = {}

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return _REPORT

    class Client:
        async def get(self, url, *, params, headers):
            observed.update(url=url, params=params, headers=headers)
            return Response()

    async def governed_async_client(**kwargs):
        observed["factory"] = kwargs
        return Client()

    async def aclose_all():
        observed["closed"] = True

    monkeypatch.setenv("ZEROTH_ECON_TOKEN", "secret-token")
    monkeypatch.setattr(diagnostic_cli, "governed_async_client", governed_async_client)
    monkeypatch.setattr(diagnostic_cli, "aclose_all", aclose_all)

    exit_code = diagnostic_cli.main(
        [
            "diagnose",
            "--workflow-id",
            "invoice-processing",
            "--base-url",
            "https://econ.example/v1/",
            "--cohort-dimension",
            "plan",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == _REPORT
    assert observed == {
        "factory": {
            "purpose": "economic-diagnostic-cli",
            "timeout": 30.0,
            "base_url": "https://econ.example/v1/",
        },
        "url": "debugger/report",
        "params": {
            "workflow_id": "invoice-processing",
            "cohort_dimension": "plan",
        },
        "headers": {"Authorization": "Bearer secret-token"},
        "closed": True,
    }


def test_diagnose_writes_a_markdown_artifact(monkeypatch, tmp_path, capsys) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return _REPORT

    class Client:
        async def get(self, _url, *, params, headers):
            assert params == {"workflow_id": "invoice-processing"}
            assert headers == {"Authorization": "Bearer secret-token"}
            return Response()

    async def governed_async_client(**kwargs):
        assert kwargs == {
            "purpose": "economic-diagnostic-cli",
            "timeout": 30.0,
            "base_url": "http://127.0.0.1:8001/v1/",
        }
        return Client()

    async def aclose_all():
        return None

    output = tmp_path / "diagnostic.md"
    monkeypatch.setenv("ZEROTH_ECON_TOKEN", "secret-token")
    monkeypatch.setattr(diagnostic_cli, "governed_async_client", governed_async_client)
    monkeypatch.setattr(diagnostic_cli, "aclose_all", aclose_all)

    exit_code = diagnostic_cli.main(
        [
            "diagnose",
            "--workflow-id",
            "invoice-processing",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.read_text().startswith(
        "# Zeroth economic diagnostic: invoice-processing\n"
    )
    assert "Wrote" in capsys.readouterr().err
    assert "savings opportunity" not in output.read_text().lower()


def test_diagnose_refuses_to_put_a_missing_secret_on_the_command_line(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ZEROTH_ECON_TOKEN", raising=False)

    exit_code = diagnostic_cli.main(
        ["diagnose", "--workflow-id", "invoice-processing"]
    )

    assert exit_code == 2
    assert "set ZEROTH_ECON_TOKEN" in capsys.readouterr().err


def test_bill_markdown_leads_with_closure_and_keeps_variance_visible() -> None:
    rendered = diagnostic_cli.render_bill_markdown(_BILL_REPORT)

    assert rendered.startswith("# Zeroth provider bill: openai / openai-2026-08\n")
    assert "**Reconciliation state:** allocated with variance" in rendered
    assert "| Provider billed total | $0.60000000 |" in rendered
    assert "| Telemetry variance | $0.10000000 |" in rendered
    assert "invoice-processing" in rendered
    assert "success" in rendered
    assert "Estimated telemetry never becomes provider truth" in rendered


def test_reconcile_imports_a_local_statement_then_writes_the_closure_report(
    monkeypatch, tmp_path, capsys
) -> None:
    statement = {
        "statement_id": "openai-2026-08",
        "provider": "openai",
        "period_start": "2026-08-01T00:00:00Z",
        "period_end": "2026-09-01T00:00:00Z",
        "currency": "USD",
        "billed_total_usd": "0.60",
        "source_kind": "cost_api",
        "buckets": [
            {
                "bucket_id": "project-a",
                "period_start": "2026-08-01T00:00:00Z",
                "period_end": "2026-09-01T00:00:00Z",
                "amount_usd": "0.60",
                "model": None,
                "provider_dimensions": {"project_id": "proj_a"},
            }
        ],
    }
    statement_path = tmp_path / "statement.json"
    statement_path.write_text(json.dumps(statement), encoding="utf-8")
    output = tmp_path / "reconciliation.md"
    observed = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class Client:
        async def post(self, url, *, json, headers):
            observed.append(("post", url, json, headers))
            return Response(
                {
                    "provider": "openai",
                    "statement_id": "openai-2026-08",
                    "statement_digest": "sha256:abc",
                }
            )

        async def get(self, url, *, headers):
            observed.append(("get", url, headers))
            return Response(_BILL_REPORT)

    async def governed_async_client(**kwargs):
        observed.append(("factory", kwargs))
        return Client()

    async def aclose_all():
        observed.append(("closed",))

    monkeypatch.setenv("ZEROTH_ECON_TOKEN", "secret-token")
    monkeypatch.setattr(diagnostic_cli, "governed_async_client", governed_async_client)
    monkeypatch.setattr(diagnostic_cli, "aclose_all", aclose_all)

    exit_code = diagnostic_cli.main(
        [
            "reconcile",
            "--statement",
            str(statement_path),
            "--base-url",
            "https://econ.example/v1",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert output.read_text().startswith(
        "# Zeroth provider bill: openai / openai-2026-08\n"
    )
    assert "Wrote" in capsys.readouterr().err
    assert observed == [
        (
            "factory",
            {
                "purpose": "provider-bill-reconciliation-cli",
                "timeout": 30.0,
                "base_url": "https://econ.example/v1/",
            },
        ),
        (
            "post",
            "reconciliation/provider-bills",
            statement,
            {"Authorization": "Bearer secret-token"},
        ),
        (
            "get",
            "reconciliation/provider-bills/openai/openai-2026-08/report",
            {"Authorization": "Bearer secret-token"},
        ),
        ("closed",),
    ]


def test_normalize_openai_costs_converts_a_complete_official_page_without_network(
    tmp_path, capsys
) -> None:
    source = tmp_path / "openai-costs.json"
    source.write_text(json.dumps(_OPENAI_COSTS_PAGE), encoding="utf-8")
    output = tmp_path / "provider-statement.json"

    exit_code = diagnostic_cli.main(
        [
            "normalize-openai-costs",
            "--input",
            str(source),
            "--statement-id",
            "openai-2026-08",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    statement = json.loads(output.read_text(encoding="utf-8"))
    ProviderBillImportRequest.model_validate(statement)
    assert statement == {
        "statement_id": "openai-2026-08",
        "provider": "openai",
        "period_start": "2026-08-01T00:00:00Z",
        "period_end": "2026-08-03T00:00:00Z",
        "currency": "USD",
        "billed_total_usd": "0.50",
        "source_kind": "cost_api",
        "buckets": [
            {
                "bucket_id": "openai:1785542400:1785628800:8021fa4a3fc2",
                "period_start": "2026-08-01T00:00:00Z",
                "period_end": "2026-08-02T00:00:00Z",
                "amount_usd": "0.10",
                "model": None,
                "provider_dimensions": {"project_id": "proj_a"},
            },
            {
                "bucket_id": "openai:1785542400:1785628800:9b5b49b98059",
                "period_start": "2026-08-01T00:00:00Z",
                "period_end": "2026-08-02T00:00:00Z",
                "amount_usd": "0.25",
                "model": None,
                "provider_dimensions": {"project_id": "proj_b"},
            },
            {
                "bucket_id": "openai:1785628800:1785715200:8021fa4a3fc2",
                "period_start": "2026-08-02T00:00:00Z",
                "period_end": "2026-08-03T00:00:00Z",
                "amount_usd": "0.15",
                "model": None,
                "provider_dimensions": {"project_id": "proj_a"},
            },
        ],
    }
    assert "Wrote" in capsys.readouterr().err


def test_normalize_openai_costs_refuses_incomplete_pagination(tmp_path, capsys) -> None:
    source = tmp_path / "openai-costs.json"
    source.write_text(
        json.dumps({**_OPENAI_COSTS_PAGE, "has_more": True, "next_page": "page_2"}),
        encoding="utf-8",
    )
    output = tmp_path / "provider-statement.json"

    exit_code = diagnostic_cli.main(
        [
            "normalize-openai-costs",
            "--input",
            str(source),
            "--statement-id",
            "openai-2026-08",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert "incomplete page" in capsys.readouterr().err
    assert not output.exists()


def test_normalize_openai_costs_refuses_unsupported_money_or_mixed_project_scope(
    tmp_path, capsys
) -> None:
    for name, page, error in (
        (
            "currency",
            {
                **_OPENAI_COSTS_PAGE,
                "data": [
                    {
                        **_OPENAI_COSTS_PAGE["data"][0],
                        "results": [
                            {
                                **_OPENAI_COSTS_PAGE["data"][0]["results"][0],
                                "amount": {"value": 0.06, "currency": "eur"},
                            }
                        ],
                    }
                ],
            },
            "USD",
        ),
        (
            "precision",
            {
                **_OPENAI_COSTS_PAGE,
                "data": [
                    {
                        **_OPENAI_COSTS_PAGE["data"][0],
                        "results": [
                            {
                                **_OPENAI_COSTS_PAGE["data"][0]["results"][0],
                                "amount": {"value": 0.000000001, "currency": "usd"},
                            }
                        ],
                    }
                ],
            },
            "8-decimal precision",
        ),
        (
            "range",
            {
                **_OPENAI_COSTS_PAGE,
                "data": [
                    {
                        **_OPENAI_COSTS_PAGE["data"][0],
                        "results": [
                            {
                                **_OPENAI_COSTS_PAGE["data"][0]["results"][0],
                                "amount": {"value": 10000000000, "currency": "usd"},
                            }
                        ],
                    }
                ],
            },
            "10 whole-number digits",
        ),
        (
            "scope",
            {
                **_OPENAI_COSTS_PAGE,
                "data": [
                    {
                        **_OPENAI_COSTS_PAGE["data"][0],
                        "results": [
                            _OPENAI_COSTS_PAGE["data"][0]["results"][0],
                            {
                                **_OPENAI_COSTS_PAGE["data"][0]["results"][1],
                                "project_id": None,
                            },
                        ],
                    }
                ],
            },
            "mixes project-scoped and unscoped",
        ),
    ):
        source = tmp_path / f"{name}.json"
        source.write_text(json.dumps(page), encoding="utf-8")
        output = tmp_path / f"{name}-statement.json"

        exit_code = diagnostic_cli.main(
            [
                "normalize-openai-costs",
                "--input",
                str(source),
                "--statement-id",
                "openai-2026-08",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 2
        assert error in capsys.readouterr().err
        assert not output.exists()
