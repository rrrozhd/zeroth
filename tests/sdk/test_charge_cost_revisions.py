"""The SDK and service preserve source-time and exact cost assertions."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from zeroth.econ.charge_costs import ChargeCostRevision as ServerRevision
from zeroth.protocol import ChargeCostRevision as SdkRevision


@pytest.mark.parametrize("model", [ServerRevision, SdkRevision])
@pytest.mark.parametrize("amount", ["0", "0.00000001", "9999999999.12345678", None])
def test_revision_protocol_preserves_money_and_aware_source_time(model, amount):
    payload = {
        "charge_id": "provider:account:request", "token_cost_usd": amount,
        "cost_measurement": "measured" if amount is not None else "unmeasured",
        "reason": "provider statement correction",
        "asserted_at": datetime(2026, 9, 6, 5, tzinfo=timezone(timedelta(hours=5))),
    }
    revision = model.model_validate(payload)
    assert revision.asserted_at == datetime(2026, 9, 6, tzinfo=UTC)
    assert revision.token_cost_usd == (Decimal(amount) if amount is not None else None)
    assert ServerRevision.model_validate_json(revision.model_dump_json()).model_dump() == revision.model_dump()
    assert SdkRevision.model_validate_json(revision.model_dump_json()).model_dump() == revision.model_dump()


@pytest.mark.parametrize("model", [ServerRevision, SdkRevision])
@pytest.mark.parametrize("change", [
    {"token_cost_usd": 0.1}, {"token_cost_usd": 9999999999.12345678},
    {"token_cost_usd": "-0.01"}, {"token_cost_usd": "0.000000001"},
    {"token_cost_usd": "10000000000"}, {"token_cost_usd": "NaN"},
    {"token_cost_usd": None}, {"cost_measurement": "unmeasured"},
    {"asserted_at": datetime(2026, 9, 6)}, {"charge_id": ""},
    {"reason": ""}, {"metadata": {"corrected": True}},
])
def test_revision_protocol_rejects_ambiguous_assertions(model, change):
    with pytest.raises(ValidationError):
        model.model_validate({
            "charge_id": "owned", "token_cost_usd": "0.50", "cost_measurement": "measured",
            "reason": "source correction", "asserted_at": datetime(2026, 9, 6, tzinfo=UTC),
            **change,
        })


def test_sdk_revision_routes_preserve_the_payload():
    import json
    from tests.sdk.test_client import _recording_client

    received = []
    client = _recording_client(received)
    payload = SdkRevision(
        charge_id="owned", asserted_at=datetime(2026, 9, 6, tzinfo=UTC),
        token_cost_usd="0.75", cost_measurement="measured", reason="repricing",
    )
    client.record_charge_cost_revision(payload)
    client.list_charge_cost_revisions("owned", limit=25)
    assert [request.url.path for request in received] == ["/v1/charge-cost-revisions"] * 2
    assert json.loads(received[0].read()) == payload.model_dump(mode="json")
    assert dict(received[1].url.params) == {"charge_id": "owned", "limit": "25"}
