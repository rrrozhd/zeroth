from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from release.cloud_funnel_report import build_funnel_report, main
from zeroth.econ.plane.backtesting.models import EconomicBacktestRecord
from zeroth.econ.plane.cloud.models import CloudSubscription, CloudTenantBinding
from zeroth.econ.plane.database import Base


NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _seed(session: Session) -> None:
    tenants = [
        ("tenant_a", NOW - timedelta(days=20)),
        ("tenant_b", NOW - timedelta(days=10)),
        ("tenant_c", NOW - timedelta(days=5)),
        ("tenant_old", NOW - timedelta(days=60)),
    ]
    for tenant_id, created_at in tenants:
        session.add(
            CloudTenantBinding(
                local_tenant_id=tenant_id,
                provider="workos",
                external_organization_id=f"org_{tenant_id}",
                created_at=created_at,
            )
        )
    session.add_all(
        [
            _subscription("tenant_a", "solo", "active", external=True),
            _subscription("tenant_b", "solo", "trialing", external=True),
            _subscription("tenant_c", "trial", "canceled", external=True),
            _subscription("tenant_old", "solo", "active", external=True),
            _backtest("tenant_a", "one", NOW - timedelta(days=20) + timedelta(hours=1), "pass"),
            _backtest("tenant_a", "two", NOW - timedelta(days=2), "pass"),
            _backtest("tenant_b", "one", NOW - timedelta(days=10) + timedelta(hours=4), "fail"),
            _backtest("tenant_old", "one", NOW - timedelta(days=59), "pass"),
        ]
    )
    session.commit()


def _subscription(
    tenant_id: str,
    plan: str,
    status: str,
    *,
    external: bool,
) -> CloudSubscription:
    return CloudSubscription(
        tenant_id=tenant_id,
        plan=plan,
        status=status,
        period_start=NOW - timedelta(days=5),
        period_end=NOW + timedelta(days=25),
        external_customer_id=f"ctm_{tenant_id}" if external else None,
        external_subscription_id=f"sub_{tenant_id}" if external else None,
        billing_provider="paddle" if external else None,
        external_price_id="pri_solo" if external else None,
        last_billing_event_id=f"evt_{tenant_id}" if external else None,
        last_billing_event_at=NOW - timedelta(days=1) if external else None,
        updated_at=NOW - timedelta(days=1),
    )


def _backtest(
    tenant_id: str,
    suffix: str,
    evaluated_at: datetime,
    verdict: str,
) -> EconomicBacktestRecord:
    return EconomicBacktestRecord(
        backtest_id=f"bkt_{tenant_id}_{suffix}",
        tenant_id=tenant_id,
        request_digest=(tenant_id + suffix).ljust(64, "x"),
        workflow="checkout",
        baseline_version="v1",
        node_id="charge",
        incumbent_model="incumbent",
        candidate_model="candidate",
        verdict=verdict,
        provider_call_credits=10,
        report_json={"verdict": verdict},
        period_start=NOW - timedelta(days=5),
        evaluated_at=evaluated_at,
        evaluated_by="sdk",
    )


def _session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'funnel.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    _seed(session)
    return session


def test_report_measures_the_self_serve_funnel_without_identifiers(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        report = build_funnel_report(session, as_of=NOW, window_days=30)

    assert report["stages"] == {
        "signed_up": 3,
        "first_value": 2,
        "repeat_value": 1,
        "checkout_completed": 3,
        "paid_active": 1,
        "canceled": 1,
        "past_due": 0,
    }
    assert report["conversion"] == {
        "signup_to_first_value_pct": 66.67,
        "signup_to_checkout_completed_pct": 100.0,
        "signup_to_paid_active_pct": 33.33,
        "first_value_to_paid_active_pct": 50.0,
    }
    assert report["time_to_first_value_hours"] == {"median": 2.5, "p90": 4.0}
    assert report["backtest_verdicts"] == {"fail": 1, "pass": 2}
    assert report["subscription_statuses"] == {"active": 1, "canceled": 1, "trialing": 1}
    encoded = json.dumps(report)
    assert "tenant_a" not in encoded
    assert "org_tenant" not in encoded
    assert "ctm_" not in encoded


def test_empty_window_uses_null_rates_instead_of_false_zero_success(tmp_path: Path) -> None:
    with _session(tmp_path) as session:
        report = build_funnel_report(session, as_of=NOW, window_days=1)

    assert report["stages"]["signed_up"] == 0
    assert set(report["conversion"].values()) == {None}
    assert report["time_to_first_value_hours"] == {"median": None, "p90": None}


def test_cli_writes_a_mode_0600_aggregate_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session = _session(tmp_path)
    monkeypatch.setattr("release.cloud_funnel_report._session", lambda: session)
    monkeypatch.setattr("release.cloud_funnel_report._now", lambda: NOW)
    output = tmp_path / "evidence" / "funnel.json"

    result = main(["--output", str(output), "--window-days", "30"])

    assert result == 0
    assert json.loads(output.read_text())["stages"]["signed_up"] == 3
    assert output.stat().st_mode & 0o777 == 0o600
    assert list(output.parent.glob(".*.tmp")) == []
    session.close()
