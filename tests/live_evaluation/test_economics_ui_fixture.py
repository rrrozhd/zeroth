from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import pytest

from release.live_evaluation.economics_ui_fixture import (
    ESTIMATED_COST_USD,
    FIXTURE_MODEL,
    FIXTURE_TENANT,
    MEASURED_COST_USD,
    PRIMARY_STATE_ROOT,
    UI_ACCESS_VALUE,
    assert_disposable_state_root,
    fixture_environment,
    seed_economics_records,
)
from zeroth.econ.analytics.unit_economics import unit_economics
from zeroth.platform.measurement import MeasurementState


def test_primary_campaign_state_root_is_rejected() -> None:
    with pytest.raises(ValueError, match="primary campaign state root"):
        assert_disposable_state_root(PRIMARY_STATE_ROOT)


def test_disposable_state_root_requires_fixture_prefix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="economics-ui-fixture"):
        assert_disposable_state_root(tmp_path / "unrelated")


def test_fixture_environment_is_isolated_provider_free_and_deterministic(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "economics-ui-fixture-test"

    environment = fixture_environment(
        state_root,
        console_origin="http://127.0.0.1:3000",
    )

    assert environment["ZEROTH_DATABASE__SQLITE_PATH"] == str(state_root / "zeroth.db")
    assert environment["ECP_DATABASE_URL"].endswith("/econ.db")
    assert str(PRIMARY_STATE_ROOT) not in json.dumps(environment, sort_keys=True)
    assert not any(name.startswith("OPENAI_") for name in environment)
    assert environment["ZEROTH_CONSOLE_CORS_ORIGINS"] == "http://127.0.0.1:3000"
    assert environment["ZEROTH_AUTO_AGENT_RUNNERS"] == "false"
    credentials = json.loads(environment["ZEROTH_SERVICE_API_KEYS_JSON"])
    assert credentials == [
        {
            "credential_id": "economics-ui-fixture-browser",
            "secret": UI_ACCESS_VALUE,
            "subject": "economics-ui-fixture-browser",
            "roles": ["admin"],
            "tenant_id": FIXTURE_TENANT,
        }
    ]


def test_fixture_environment_rejects_non_loopback_console_origin(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        fixture_environment(
            tmp_path / "economics-ui-fixture-test",
            console_origin="https://console.example.com",
        )


def test_cli_bootstrap_binds_econ_database_before_package_import(tmp_path: Path) -> None:
    state_root = tmp_path / "economics-ui-fixture-subprocess"
    working_directory = tmp_path / "working-directory"
    working_directory.mkdir()
    environment = dict(os.environ)
    environment.pop("ECP_DATABASE_URL", None)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parents[2] / "scripts/economics_ui_fixture.py"),
            "serve",
            "--state-root",
            str(state_root),
            "--port",
            str(port),
        ],
        cwd=working_directory,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _, stderr = process.communicate()
                pytest.fail(f"fixture exited before health check: {stderr}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    assert response.status == 200
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("fixture did not become healthy")

        cost_request = Request(
            f"http://127.0.0.1:{port}/v1/tenants/{FIXTURE_TENANT}/cost",
            headers={"X-API-Key": UI_ACCESS_VALUE},
        )
        with urlopen(cost_request, timeout=3) as response:
            ledger = json.load(response)
        assert ledger["actual_spend_usd"] == MEASURED_COST_USD
        assert ledger["paid_spend_usd"] == MEASURED_COST_USD
        assert ledger["estimated_spend_usd"] == 0
    finally:
        process.terminate()
        process.wait(timeout=15)

    assert not (working_directory / "econ_plane.db").exists()
    assert (state_root / "econ.db").is_file()
    assert (state_root / "zeroth.db").is_file()


@pytest.mark.asyncio
async def test_seed_creates_separate_measured_and_estimated_economics(sqlite_db) -> None:
    fixture = await seed_economics_records(sqlite_db)

    assert fixture.measured_run.status.value == "COMPLETED"
    assert fixture.failed_run.status.value == "FAILED"
    assert fixture.measured_audit.cost_usd == MEASURED_COST_USD
    assert fixture.measured_audit.estimated_cost_usd is None
    assert fixture.measured_audit.cost_measurement is MeasurementState.MEASURED
    assert fixture.failed_audit.cost_usd is None
    assert fixture.failed_audit.estimated_cost_usd == ESTIMATED_COST_USD
    assert fixture.failed_audit.cost_measurement is MeasurementState.ESTIMATED
    assert fixture.measured_audit.token_usage is not None
    assert fixture.failed_audit.token_usage is not None
    assert fixture.measured_audit.token_usage.model_name == FIXTURE_MODEL
    assert fixture.failed_audit.token_usage.model_name == FIXTURE_MODEL

    report = unit_economics(
        [fixture.measured_run, fixture.failed_run],
        [fixture.measured_audit, fixture.failed_audit],
    )
    assert report.total_cost_usd == MEASURED_COST_USD
    assert report.estimated_total_cost_usd == ESTIMATED_COST_USD
    assert report.failure_tax_usd == 0.0
    assert report.estimated_failure_tax_usd == ESTIMATED_COST_USD
    assert report.cost_per_successful_run_usd == MEASURED_COST_USD
    assert report.estimated_cost_per_successful_run_usd == ESTIMATED_COST_USD


@pytest.mark.asyncio
async def test_seed_is_idempotent(sqlite_db) -> None:
    first = await seed_economics_records(sqlite_db)
    second = await seed_economics_records(sqlite_db)

    assert second.measured_run.run_id == first.measured_run.run_id
    assert second.failed_run.run_id == first.failed_run.run_id
    assert second.measured_audit.audit_id == first.measured_audit.audit_id
    assert second.failed_audit.audit_id == first.failed_audit.audit_id
