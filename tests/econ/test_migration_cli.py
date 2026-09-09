from __future__ import annotations

import json

import httpx
import pytest

from zeroth.service import economic_diagnostic_cli as cli


class Response:
    def __init__(
        self,
        status: int = 200,
        payload=None,
        content: bytes | None = None,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ):
        self.status_code = status
        self._payload = payload
        self.content = content if content is not None else json.dumps(payload).encode()
        self.headers = {"x-request-id": "request-safe", **(headers or {})}
        self.chunks = chunks
        self.body_iterated = False

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def aiter_bytes(self):
        self.body_iterated = True
        for chunk in self.chunks or [self.content]:
            yield chunk

    async def aiter_raw(self):
        self.body_iterated = True
        for chunk in self.chunks or [self.content]:
            yield chunk


class Stream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Stream(self.responses.pop(0))


def install_client(monkeypatch, responses):
    client = Client(responses)

    async def factory(**kwargs):
        client.factory = kwargs
        return client

    async def close():
        client.closed = True

    monkeypatch.setenv("ZEROTH_ECON_TOKEN", "secret-token")
    monkeypatch.setattr(cli, "governed_async_client", factory)
    monkeypatch.setattr(cli, "aclose_all", close)
    return client


def decision(*, verdict="abstain", action="collect_evidence"):
    return {
        "workload": "invoice-agent",
        "incumbent_model": "model-a",
        "candidate_model": "model-b",
        "verdict": verdict,
        "recommended_action": action,
        "recommended_candidate_share": 0.0,
        "recommended_routing": {},
        "reason_codes": ["calibration_required"],
        "additional_cases_required": 20,
        "simulations": 100,
        "seed": 7,
        "actions": [],
        "forecast_readiness": {
            "calibration_state": "unknown",
            "drift_state": "unknown",
            "metrics": [],
        },
        "evidence_lineage": {"algorithm_version": "bounded-v2"},
        "decision_id": "pdec-safe",
        "evaluated_at": "2026-09-04T00:00:00Z",
    }


def test_migration_help_exposes_the_frozen_command_set() -> None:
    parser = cli.build_parser()
    migration = next(
        action for action in parser._actions if getattr(action, "dest", None) == "command"
    ).choices["migration"]
    commands = next(
        action
        for action in migration._actions
        if getattr(action, "dest", None) == "migration_command"
    ).choices
    assert set(commands) == {
        "evaluate",
        "refresh",
        "history",
        "report",
        "schedule-create",
        "schedule-deactivate",
        "rollout-create",
        "rollout-assign",
        "rollout-verify",
        "rollout-stop",
    }


def test_evaluate_uses_environment_token_and_round_trips_json(
    monkeypatch, tmp_path, capsys
) -> None:
    payload = {"evidence": {}, "policy": {}, "simulations": 100, "seed": 7}
    source = tmp_path / "request.json"
    source.write_text(json.dumps(payload))
    result = decision()
    client = install_client(monkeypatch, [Response(payload=result)])

    code = cli.main(
        [
            "migration",
            "evaluate",
            "--input",
            str(source),
            "--format",
            "json",
            "--base-url",
            "https://econ.example/v1",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == result
    assert client.factory == {
        "purpose": "economic-migration-cli",
        "timeout": 30.0,
        "base_url": "https://econ.example/v1/",
    }
    assert client.calls == [
        (
            "POST",
            "decisions/model-migration",
            {
                "headers": {
                    "Authorization": "Bearer secret-token",
                    "Accept-Encoding": "identity",
                },
                "json": payload,
            },
        )
    ]


def test_abstention_markdown_is_advisory_and_evidence_first() -> None:
    rendered = cli.render_migration_markdown(decision())
    assert rendered.startswith("# Migration decision: ABSTAIN\n")
    assert "**Advisory action:** collect evidence" in rendered
    assert "**Additional evidence required:** 20 cases" in rendered
    assert rendered.index("Additional evidence") < rendered.index("Candidate traffic share")
    assert "authorization" not in rendered.lower()
    assert "ship" not in rendered.lower()
    assert "unavailable" in rendered.lower()


def test_recommendation_markdown_renders_risk_and_customer_limits() -> None:
    value = decision(verdict="recommend", action="hybrid_route")
    value["recommended_candidate_share"] = 0.25
    value["actions"] = [
        {
            "candidate_share": 0.25,
            "monthly_savings_p05_usd": "10",
            "monthly_savings_p95_usd": "20",
            "probability_quality_breach": 0.01,
            "probability_latency_breach": 0.02,
            "probability_critical_error_breach": 0.03,
            "cvar_loss_usd": "4",
            "minimum_quality_drop_tolerance": 0.04,
            "minimum_p95_latency_limit_ms": 900,
            "minimum_critical_error_rate_limit": 0.05,
            "minimum_cvar_loss_limit_usd": "5",
            "feasible": True,
            "violated_constraints": [],
        }
    ]
    rendered = cli.render_migration_markdown(value)
    assert "Quality breach probability: 0.01" in rendered
    assert "CVaR loss: 4" in rendered
    assert "Quality-drop tolerance: 0.04" in rendered
    assert "P95 latency limit: 900" in rendered
    assert "Uncertainty qualification: feasible" in rendered


def test_markdown_preserves_actual_readiness_and_infeasible_states() -> None:
    value = decision(verdict="recommend", action="hybrid_route")
    value["forecast_readiness"] = {
        "calibration_state": "calibrated",
        "drift_state": "warning",
        "metrics": [],
    }
    value["recommended_candidate_share"] = 0.25
    value["actions"] = [{"candidate_share": 0.25, "feasible": False}]

    rendered = cli.render_migration_markdown(value)

    assert "**Calibration state:** calibrated" in rendered
    assert "**Drift state:** warning" in rendered
    assert "Uncertainty qualification: infeasible" in rendered


@pytest.mark.parametrize(
    "subcommand,path,id_flag",
    [
        ("refresh", "decisions/model-migration/refresh", None),
        ("schedule-create", "probabilistic-decision-schedules", None),
        (
            "schedule-deactivate",
            "probabilistic-decision-schedules/sched-1/deactivate",
            "--schedule-id",
        ),
        ("rollout-create", "randomized-rollouts", None),
        ("rollout-assign", "randomized-rollouts/roll-1/assignments", "--rollout-id"),
        ("rollout-verify", "randomized-rollouts/roll-1/verify", "--rollout-id"),
        ("rollout-stop", "randomized-rollouts/roll-1/stop", "--rollout-id"),
    ],
)
def test_mutation_commands_call_only_the_documented_api(
    monkeypatch, tmp_path, capsys, subcommand, path, id_flag
) -> None:
    source = tmp_path / "input.json"
    source.write_text('{"field":"exact"}')
    client = install_client(monkeypatch, [Response(payload={"status": "retained"})])
    argv = ["migration", subcommand]
    if id_flag:
        argv.extend([id_flag, "sched-1" if id_flag == "--schedule-id" else "roll-1"])
    if subcommand not in {"schedule-deactivate", "rollout-stop"}:
        argv.extend(["--input", str(source)])
    argv.extend(["--format", "json"])

    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "retained"}
    assert client.calls[0][0:2] == ("POST", path)
    assert "Authorization" in client.calls[0][2]["headers"]


def test_history_uses_bounded_query_parameters(monkeypatch, capsys) -> None:
    client = install_client(monkeypatch, [Response(payload=[decision()])])
    assert (
        cli.main(
            ["migration", "history", "--workload", "invoice", "--limit", "25", "--format", "json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == [decision()]
    assert client.calls[0] == (
        "GET",
        "decisions/model-migrations",
        {
            "headers": {
                "Authorization": "Bearer secret-token",
                "Accept-Encoding": "identity",
            },
            "params": {"workload": "invoice", "limit": 25},
        },
    )


def test_report_downloads_exact_bytes_then_explicitly_delivers(
    monkeypatch, tmp_path, capsys
) -> None:
    pdf = b"%PDF-1.4\nexact\n%%EOF\n"
    metadata = {
        "report_id": "report-1",
        "decision_id": "pdec-safe",
        "sha256": "digest",
        "download_path": "/v1/reports/report-1",
    }
    delivered = {"status": "sent", "delivery_mode": "attachment"}
    client = install_client(
        monkeypatch,
        [Response(201, metadata), Response(200, content=pdf), Response(201, delivered)],
    )
    output = tmp_path / "decision.pdf"

    code = cli.main(
        [
            "migration",
            "report",
            "--decision-id",
            "pdec-safe",
            "--output",
            str(output),
            "--recipient",
            "owner@example.com",
            "--delivery-mode",
            "attachment",
            "--format",
            "json",
        ]
    )

    assert code == 0
    assert output.read_bytes() == pdf
    assert json.loads(capsys.readouterr().out)["delivery"] == delivered
    assert [(method, path) for method, path, _ in client.calls] == [
        ("POST", "decisions/pdec-safe/reports"),
        ("GET", "reports/report-1"),
        ("POST", "reports/report-1/deliveries"),
    ]
    assert client.calls[-1][2]["json"] == {
        "recipients": ["owner@example.com"],
        "delivery_mode": "attachment",
    }

    second = install_client(monkeypatch, [])
    assert (
        cli.main(["migration", "report", "--decision-id", "pdec-safe", "--output", str(output)])
        == 2
    )
    assert second.calls == []


@pytest.mark.parametrize("status", [401, 402, 403, 404, 409, 422, 429, 500])
def test_remote_failures_are_bounded_and_secret_free(monkeypatch, capsys, status) -> None:
    marker = "private-cross-tenant-id secret-token SELECT credentials"
    install_client(monkeypatch, [Response(status, {"detail": marker})])
    code = cli.main(["migration", "history", "--workload", marker])
    captured = capsys.readouterr()
    assert code == 1
    assert f"HTTP {status}" in captured.err
    assert marker not in captured.err
    assert "secret-token" not in captured.err
    assert captured.out == ""


def test_transport_and_local_input_failures_have_distinct_safe_codes(
    monkeypatch, tmp_path, capsys
) -> None:
    class FailingClient:
        def stream(self, method, url, **kwargs):
            class FailingStream:
                async def __aenter__(self):
                    request = httpx.Request(method, f"https://secret-token.invalid/{url}")
                    raise httpx.ConnectError("secret-token private-host", request=request)

                async def __aexit__(self, exc_type, exc, traceback):
                    return False

            return FailingStream()

    async def factory(**kwargs):
        return FailingClient()

    async def close():
        return None

    monkeypatch.setenv("ZEROTH_ECON_TOKEN", "secret-token")
    monkeypatch.setattr(cli, "governed_async_client", factory)
    monkeypatch.setattr(cli, "aclose_all", close)
    assert cli.main(["migration", "history"]) == 1
    assert "secret-token" not in capsys.readouterr().err

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b"x" * (1024 * 1024 + 1))
    assert cli.main(["migration", "evaluate", "--input", str(oversized)]) == 2
    assert "too large" in capsys.readouterr().err


def test_report_output_parent_is_validated_before_remote_mutation(monkeypatch, tmp_path) -> None:
    client = install_client(monkeypatch, [])
    output = tmp_path / "missing" / "report.pdf"
    assert cli.main(["migration", "report", "--decision-id", "safe", "--output", str(output)]) == 2
    assert client.calls == []


def test_migration_input_must_be_a_direct_regular_file(monkeypatch, tmp_path, capsys) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    install_client(monkeypatch, [])
    assert cli.main(["migration", "evaluate", "--input", str(link)]) == 2
    assert "regular file" in capsys.readouterr().err


def test_migration_input_bounds_the_open_descriptor_read(monkeypatch, tmp_path) -> None:
    source = tmp_path / "growing.json"
    source.write_text("{}")
    chunks = iter([b"x" * cli._MAX_MIGRATION_JSON_BYTES, b"x"])
    monkeypatch.setattr(cli.os, "read", lambda descriptor, size: next(chunks))

    with pytest.raises(ValueError, match="too large"):
        cli._migration_input(str(source))


@pytest.mark.parametrize("token_env", ["BAD-NAME", "1TOKEN", "A" * 129])
def test_token_environment_name_is_bounded_and_safe(monkeypatch, capsys, token_env) -> None:
    monkeypatch.setenv(token_env, "secret-token")
    assert cli.main(["migration", "history", "--token-env", token_env]) == 2
    captured = capsys.readouterr()
    assert token_env not in captured.err
    assert "secret-token" not in captured.err


@pytest.mark.parametrize("base_url", ["http://[", "https://safe.example/\nforged", "x" * 2049])
def test_malformed_base_urls_are_bounded_local_failures(monkeypatch, capsys, base_url) -> None:
    monkeypatch.setenv("ZEROTH_ECON_TOKEN", "secret-token")
    assert cli.main(["migration", "history", "--base-url", base_url]) == 2
    captured = capsys.readouterr()
    assert base_url not in captured.err
    assert "secret-token" not in captured.err


@pytest.mark.parametrize(
    "identifier",
    [".", "..", "a/b", "a\\b", "line\nbreak", "escape\x1bvalue", "unicode\u202eseparator"],
)
def test_migration_identifiers_cannot_escape_one_http_path_segment(identifier) -> None:
    with pytest.raises(ValueError):
        cli._migration_identifier(identifier, field="identifier")


@pytest.mark.parametrize(
    "identifier", ["pdec_safe-1", "sched_123", "550e8400-e29b-41d4-a716-446655440000"]
)
def test_valid_migration_identifiers_build_under_the_configured_api_path(identifier) -> None:
    encoded = cli._migration_identifier(identifier, field="identifier")
    request = httpx.Client(base_url="https://econ.example/v1/").build_request(
        "POST", f"randomized-rollouts/{encoded}/stop"
    )
    assert request.url == f"https://econ.example/v1/randomized-rollouts/{identifier}/stop"


@pytest.mark.parametrize(
    "request_id",
    ["safe\nFORGED", "safe\rFORGED", "safe\x1bFORGED", "safe\u202eFORGED"],
)
def test_hostile_request_ids_are_discarded(monkeypatch, capsys, request_id) -> None:
    response = Response(500, {"detail": "private"})
    response.headers["x-request-id"] = request_id
    install_client(monkeypatch, [response])
    assert cli.main(["migration", "history"]) == 1
    assert request_id not in capsys.readouterr().err


def test_malformed_remote_report_id_is_a_remote_failure(monkeypatch, tmp_path, capsys) -> None:
    install_client(
        monkeypatch,
        [Response(201, {"report_id": "..", "decision_id": "safe"})],
    )
    assert (
        cli.main(
            ["migration", "report", "--decision-id", "safe", "--output", str(tmp_path / "x.pdf")]
        )
        == 1
    )
    assert "invalid remote response" in capsys.readouterr().err


def test_oversized_success_json_is_rejected_without_output(monkeypatch, capsys) -> None:
    install_client(monkeypatch, [Response(200, {"value": "x" * (2 * 1024 * 1024)})])
    assert cli.main(["migration", "history", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "xxxx" not in captured.err


def test_oversized_pdf_is_rejected_without_creating_output(monkeypatch, tmp_path) -> None:
    output = tmp_path / "large.pdf"
    install_client(
        monkeypatch,
        [
            Response(201, {"report_id": "report-safe"}),
            Response(200, content=b"%PDF-" + b"x" * (20 * 1024 * 1024)),
        ],
    )
    assert cli.main(["migration", "report", "--decision-id", "safe", "--output", str(output)]) == 1
    assert not output.exists()


@pytest.mark.parametrize("declared", [str(20 * 1024 * 1024 + 1), "not-a-length", "-1"])
def test_pdf_rejects_oversized_or_malformed_declared_lengths(
    monkeypatch, tmp_path, declared
) -> None:
    output = tmp_path / "declared.pdf"
    install_client(
        monkeypatch,
        [
            Response(201, {"report_id": "report-safe"}),
            Response(200, content=b"unread", headers={"content-length": declared}),
        ],
    )
    assert cli.main(["migration", "report", "--decision-id", "safe", "--output", str(output)]) == 1
    assert not output.exists()


def test_pathological_content_length_is_a_sanitized_remote_failure(
    monkeypatch, tmp_path, capsys
) -> None:
    output = tmp_path / "pathological.pdf"
    install_client(
        monkeypatch,
        [
            Response(201, {"report_id": "report-safe"}),
            Response(200, content=b"unread", headers={"content-length": "9" * 5000}),
        ],
    )
    assert cli.main(["migration", "report", "--decision-id", "safe", "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "9999" not in captured.err
    assert not output.exists()


def test_encoded_json_response_is_rejected_before_body_iteration(monkeypatch, capsys) -> None:
    response = Response(
        200,
        content=b"compressed-or-hostile",
        headers={"content-encoding": "gzip"},
    )
    install_client(monkeypatch, [response])
    assert cli.main(["migration", "history", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not response.body_iterated


def test_pdf_rejects_chunked_body_that_exceeds_lying_length(monkeypatch, tmp_path) -> None:
    output = tmp_path / "chunked.pdf"
    install_client(
        monkeypatch,
        [
            Response(201, {"report_id": "report-safe"}),
            Response(
                200,
                content=b"",
                headers={"content-length": "4"},
                chunks=[b"%PDF-", b"x" * (20 * 1024 * 1024)],
            ),
        ],
    )
    assert cli.main(["migration", "report", "--decision-id", "safe", "--output", str(output)]) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["migration", "history", "--workload", "x" * 193],
        ["migration", "history", "--workload", "safe\nforged"],
        [
            "migration",
            "report",
            "--decision-id",
            "safe",
            "--output",
            "unused.pdf",
            "--recipient",
            "x" * 321,
        ],
    ],
)
def test_operator_text_arguments_are_bounded_before_network(monkeypatch, capsys, argv) -> None:
    client = install_client(monkeypatch, [])
    assert cli.main(argv) == 2
    assert client.calls == []
    assert "forged" not in capsys.readouterr().err


def test_pdf_finalization_failure_leaves_no_partial_artifact(monkeypatch, tmp_path) -> None:
    output = tmp_path / "report.pdf"
    install_client(
        monkeypatch,
        [Response(201, {"report_id": "report-safe"}), Response(200, content=b"%PDF-exact")],
    )

    def fail_link(source, destination):
        raise OSError("simulated finalization failure")

    monkeypatch.setattr(cli.os, "link", fail_link)
    assert cli.main(["migration", "report", "--decision-id", "safe", "--output", str(output)]) == 2
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_text_finalization_failure_leaves_no_partial_artifact(
    monkeypatch, tmp_path, capsys
) -> None:
    output = tmp_path / "history.json"
    install_client(monkeypatch, [Response(200, [])])

    def fail_link(source, destination):
        raise OSError("simulated finalization failure")

    monkeypatch.setattr(cli.os, "link", fail_link)
    assert cli.main(["migration", "history", "--format", "json", "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_in_process_api_journey_covers_retention_report_schedule_and_rollout(
    monkeypatch, tmp_path, capsys
) -> None:
    from datetime import UTC, datetime

    from fastapi import FastAPI
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from zeroth.econ.analytics.service_auth import mint_econ_service_token
    from zeroth.econ.plane import config
    from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
    from zeroth.econ.plane.database import Base
    from zeroth.econ.plane.decisioning.api import router as decisioning_router
    from zeroth.econ.plane.decisioning.models import ProbabilisticMigrationDecisionRecord
    from zeroth.econ.plane.reports.api import router as reports_router
    from zeroth.econ.plane.reports.models import DecisionReportRecord
    from zeroth.econ.plane.scoped_session import ScopedSession
    from zeroth.platform.storage.scoping import TenantWideScopeContext

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'journey.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    recommended_id = "pdec_recommended_fixture"
    recommended = {
        "workload": "invoice-agent",
        "incumbent_model": "model-a",
        "candidate_model": "model-b",
        "verdict": "recommend",
        "recommended_action": "hybrid_route",
        "recommended_candidate_share": 0.5,
        "recommended_routing": {},
        "reason_codes": ["risk_constraints_satisfied"],
        "additional_cases_required": 0,
        "simulations": 100,
        "seed": 7,
        "actions": [],
        "forecast_readiness": {
            "calibration_state": "calibrated",
            "drift_state": "stable",
            "calibration_periods": 6,
            "metrics": [],
            "missing_metrics": [],
        },
        "evidence_lineage": {},
        "decision_id": recommended_id,
        "evaluated_at": now.isoformat(),
    }
    with Session(engine) as db:
        db.add(
            ProbabilisticMigrationDecisionRecord(
                decision_id=recommended_id,
                tenant_id="tenant-a",
                request_digest="f" * 64,
                workload="invoice-agent",
                incumbent_model="model-a",
                candidate_model="model-b",
                verdict="recommend",
                recommended_action="hybrid_route",
                evidence_json={},
                evidence_lineage_json={},
                policy_json={},
                report_json=recommended,
                evaluated_at=now,
                evaluated_by="fixture",
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(decisioning_router, prefix="/v1")
    app.include_router(reports_router, prefix="/v1")

    def scoped_db():
        with Session(engine) as db:
            yield ScopedSession(db, TenantWideScopeContext(tenant_id="tenant-a"))

    app.dependency_overrides[get_cloud_scoped_db] = scoped_db
    monkeypatch.setattr(config.settings, "cloud_entitlements_enabled", False)
    monkeypatch.setattr(config.settings, "service_principal_tenant_id", "tenant-a")
    token = mint_econ_service_token()
    assert token is not None
    monkeypatch.setenv("ZEROTH_ECON_TOKEN", token)
    clients = []

    async def factory(**kwargs):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=kwargs["base_url"]
        )
        clients.append(client)
        return client

    async def close():
        while clients:
            await clients.pop().aclose()

    monkeypatch.setattr(cli, "governed_async_client", factory)
    monkeypatch.setattr(cli, "aclose_all", close)

    def invoke(*args):
        assert cli.main(["migration", *args, "--format", "json"]) == 0
        return json.loads(capsys.readouterr().out)

    request = {
        "evidence": {
            "workload": "journey-workload",
            "incumbent_model": "model-a",
            "candidate_model": "model-b",
            "incumbent": [
                {
                    "case_id": "case-1",
                    "cost_usd": "1",
                    "latency_ms": 100,
                    "accepted": True,
                    "critical_error": False,
                    "source": "production",
                }
            ],
            "candidate": [
                {
                    "case_id": "case-1",
                    "cost_usd": "0.5",
                    "latency_ms": 90,
                    "accepted": True,
                    "critical_error": False,
                    "source": "replay",
                }
            ],
            "period_request_counts": [100],
            "demand_horizon": "month",
        },
        "policy": {},
        "simulations": 100,
        "seed": 7,
    }
    request_path = tmp_path / "evaluate.json"
    request_path.write_text(json.dumps(request))
    evaluated = invoke("evaluate", "--input", str(request_path))
    assert evaluated["verdict"] == "abstain"
    history = invoke("history", "--workload", "journey-workload")
    assert history[0]["decision_id"] == evaluated["decision_id"]

    pdf_path = tmp_path / "journey.pdf"
    report = invoke("report", "--decision-id", evaluated["decision_id"], "--output", str(pdf_path))
    assert report["report"]["decision_id"] == evaluated["decision_id"]
    with Session(engine) as db:
        stored = db.get(DecisionReportRecord, report["report"]["report_id"])
        assert stored is not None and stored.pdf_bytes == pdf_path.read_bytes()

    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "evidence_source": {
                    "workload": "journey-workload",
                    "incumbent_model": "model-a",
                    "candidate_model": "model-b",
                },
                "policy": {},
                "interval_minutes": 60,
                "simulations": 100,
            }
        )
    )
    schedule = invoke("schedule-create", "--input", str(schedule_path))
    stopped_schedule = invoke("schedule-deactivate", "--schedule-id", schedule["schedule_id"])
    assert stopped_schedule["active"] is False

    rollout_path = tmp_path / "rollout.json"
    rollout_path.write_text(
        json.dumps(
            {
                "decision_id": recommended_id,
                "candidate_probability": 0.5,
                "minimum_per_arm": 20,
            }
        )
    )
    rollout = invoke("rollout-create", "--input", str(rollout_path))
    assignment_path = tmp_path / "assignment.json"
    assignment_path.write_text(json.dumps({"subject_id": "subject-1"}))
    assignment = invoke(
        "rollout-assign",
        "--rollout-id",
        rollout["rollout_id"],
        "--input",
        str(assignment_path),
    )
    assert assignment["subject_id"] == "subject-1"
    verify_path = tmp_path / "verify.json"
    verify_path.write_text(json.dumps({"bootstrap_samples": 100, "seed": 7}))
    verification = invoke(
        "rollout-verify",
        "--rollout-id",
        rollout["rollout_id"],
        "--input",
        str(verify_path),
    )
    assert verification["causal_status"] == "inconclusive"
    stopped = invoke("rollout-stop", "--rollout-id", rollout["rollout_id"])
    assert stopped["active"] is False
    assert invoke("rollout-stop", "--rollout-id", rollout["rollout_id"])["active"] is False
    engine.dispose()
