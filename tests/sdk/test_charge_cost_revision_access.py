"""Charge corrections use the existing project scope, roles and event allowance."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.econ_plane.test_charge_cost_revisions import (
    engine as database_engine, client as http_client, seed, revision,
)
from tests.econ_plane.test_sdk_evidence_namespace import scoped
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db
from zeroth.econ.plane.cloud.entitlements import PLAN_CATALOG, PlanLimits
from zeroth.econ.plane.cloud.keys_api import router as keys_router
from zeroth.econ.plane.cloud.keys_schemas import ApiKeyCreate
from zeroth.econ.plane.cloud.keys_service import issue_api_key
from zeroth.econ.plane.cloud.models import CloudSubscription, CloudUsageCounter
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import get_db
from zeroth.protocol import ChargeCostRevision
from zeroth.sdk import ZerothClient

engine = database_engine
client = http_client


def test_sdk_revision_uses_real_project_key_roles_and_tenant_scope(engine, client):
    with Session(engine) as raw:
        seed(scoped(raw))

    def raw_db():
        with Session(engine) as raw:
            yield raw

    client.app.dependency_overrides[get_db] = raw_db
    client.app.include_router(keys_router, prefix="/v1")
    keys = {}
    for role in ["Admin", "Analyst", "Viewer"]:
        response = client.post("/v1/cloud/api-keys", json={"name": role, "roles": [role]})
        assert response.status_code == 200
        keys[role] = response.json()["api_key"]
    # From this point, use the real key-to-scope dependency, not a fixture scope.
    client.app.dependency_overrides.pop(get_cloud_scoped_db)

    def forward(request):
        response = client.request(
            request.method, request.url.path, params=request.url.params,
            headers=dict(request.headers), content=request.read(),
        )
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    with httpx.Client(transport=httpx.MockTransport(forward)) as transport:
        sdk = ZerothClient(api_key=keys["Analyst"], base_url="http://testserver", http_client=transport)
        assert sdk.record_charge_cost_revision(ChargeCostRevision.model_validate(revision()))["status"] == "inserted"
        assert len(sdk.list_charge_cost_revisions("account:request-1")) == 1
    viewer = {"Authorization": f"Bearer {keys['Viewer']}"}
    assert client.post("/v1/charge-cost-revisions", headers=viewer, json=revision("0.2", 2)).status_code == 403
    assert client.get("/v1/charge-cost-revisions", headers=viewer, params={"charge_id": "account:request-1"}).status_code == 200
    with Session(engine) as raw:
        foreign = issue_api_key(scoped(raw, "tenant-b"), ApiKeyCreate(name="foreign", roles=["Analyst"]), subject="test", workspace_id=None)
    headers = {"Authorization": f"Bearer {foreign.api_key}"}
    assert client.post("/v1/charge-cost-revisions", headers=headers, json=revision()).status_code == 422
    assert client.get("/v1/charge-cost-revisions", headers=headers, params={"charge_id": "account:request-1"}).json() == []


def test_revision_quota_counts_only_new_accepted_assertions(engine, client, monkeypatch):
    with Session(engine) as raw:
        seed(scoped(raw))
        now = datetime.now(UTC)
        raw.add(CloudSubscription(
            tenant_id="tenant-a", plan="trial", status="trialing",
            period_start=now - timedelta(days=1), period_end=now + timedelta(days=13),
            updated_at=now,
        ))
        raw.commit()
    monkeypatch.setattr(settings, "cloud_entitlements_enabled", True)
    monkeypatch.setitem(PLAN_CATALOG, "trial", PlanLimits(
        event_limit=1, decision_scan_limit=1, backtest_limit=1,
        backtest_call_limit=1, schedule_limit=1, minimum_schedule_interval_minutes=1440,
    ))
    assert client.post("/v1/charge-cost-revisions", json=revision(charge_id="missing")).status_code == 422
    assert client.post("/v1/charge-cost-revisions", json=revision()).status_code == 200
    repeated = client.post("/v1/charge-cost-revisions", json=revision())
    assert repeated.status_code == 200 and repeated.json()["status"] == "duplicate"
    assert client.post("/v1/charge-cost-revisions", json=revision(reason="changed")).status_code == 422
    assert client.post("/v1/charge-cost-revisions", json=revision("0.8", 2)).status_code == 402
    with Session(engine) as raw:
        assert raw.scalars(select(CloudUsageCounter.quantity).where(CloudUsageCounter.meter == "events")).one() == 1
