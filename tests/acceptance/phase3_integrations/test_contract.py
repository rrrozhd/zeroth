"""Contract node: the plain SDK path reproduces the frozen ledger and honours identity."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.acceptance.phase3_integrations import ledger, server, workload
from zeroth.econ.analytics.rightsizing import describe
from zeroth.econ.plane.instrumentation.models import ExecutionEvent as StoredExecution
from zeroth.instrumentation import Recorder, Usage
from zeroth.instrumentation.rate_card import _CARD, price, rates

HERE = Path(__file__).parent
EXPECTED = json.loads((HERE / "expected_ledger.json").read_text())["versions"]
FROZEN = ("workload.py", "expected_ledger.json", "INTERFACE.md")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Spy:
    """Record every ingestion status the server returned."""

    def __init__(self, inner):
        self.inner, self.statuses = inner, []

    def record_execution(self, event):
        response = self.inner.record_execution(event)
        self.statuses.append(response["status"])
        return response

    def record_outcome(self, event):
        response = self.inner.record_outcome(event)
        self.statuses.append(response["status"])
        return response


def stored(engine, event_id: str) -> StoredExecution:
    with Session(engine) as db:
        row = db.scalars(
            select(StoredExecution).where(StoredExecution.execution_id == event_id)
        ).one()
        db.expunge(row)
        return row


def test_frozen_inputs_match_manifest():
    manifest = json.loads((HERE / "manifest.json").read_text())
    assert {name: sha256(HERE / name) for name in FROZEN} == manifest["frozen"]


def test_reference_workload_reconciles_for_both_versions(engine, origin):
    recorder = Recorder(Spy(server.sdk(origin)))
    for version in workload.VERSIONS:
        workload.replay(recorder, version)
    assert recorder.lost == []
    assert set(recorder.client.statuses) == {"inserted"}
    assert ledger.read(engine, workload.WORKFLOW) == EXPECTED


def test_delivering_everything_again_changes_nothing(engine, origin):
    spy = Spy(server.sdk(origin))
    recorder = Recorder(spy)
    for version in workload.VERSIONS:
        workload.replay(recorder, version)
    first = len(spy.statuses)
    for version in workload.VERSIONS:
        workload.replay(recorder, version)
    duplicate = [w for w in workload.RUNS if w["id"] == "duplicate_delivery"]
    workload.replay(recorder, "v1", runs=duplicate)
    assert spy.statuses[:first].count("inserted") == first
    assert set(spy.statuses[first:]) == {"duplicate"}
    assert ledger.read(engine, workload.WORKFLOW) == EXPECTED


def test_missing_usage_and_failed_fallback_stay_unmeasured(engine, origin):
    workload.replay(Recorder(server.sdk(origin)), "v1")
    for run_id, error in (("missing_usage", None), ("fallback", "ServiceUnavailable")):
        row = stored(engine, f"{workload.WORKFLOW}:v1:{run_id}:answer:1")
        assert row.token_cost_usd is None
        assert row.cost_measurement == "unmeasured"
        assert row.event_metadata["pricing"] == "missing_usage"
        assert row.event_metadata["usage"] is None
        assert row.event_metadata["error"] == error
    served = stored(engine, f"{workload.WORKFLOW}:v1:fallback:answer:2")
    assert served.model_version == "gpt-4.1" and served.cost_measurement == "estimated"


def test_unknown_model_is_unmeasured_not_zero(engine, origin):
    assert price("vendor/unlisted-model", input_tokens=10, output_tokens=10) is None
    run = Recorder(server.sdk(origin)).run(workload.WORKFLOW, "v9", "unlisted")
    run.charge("answer", model="vendor/unlisted-model", usage=Usage(10, 10))
    row = stored(engine, f"{workload.WORKFLOW}:v9:unlisted:answer:1")
    assert row.token_cost_usd is None and row.cost_measurement == "unmeasured"
    assert row.event_metadata["pricing"] == "unknown_model"
    assert row.event_metadata["usage"] == Usage(10, 10).as_metadata()


def test_rate_card_matches_server_catalog():
    import litellm

    for model in _CARD:
        card = rates(model)
        catalog = describe(model)
        assert catalog is not None, model
        assert catalog.provider == card.provider
        assert Decimal(str(catalog.input_per_mtok_usd)) == card.input_per_mtok
        assert Decimal(str(catalog.output_per_mtok_usd)) == card.output_per_mtok
        info = litellm.get_model_info(model)
        for key, value in (
            ("cache_read_input_token_cost", card.cache_read_per_mtok),
            ("cache_creation_input_token_cost", card.cache_write_per_mtok),
        ):
            raw = info.get(key)
            if value is None:
                assert not raw, (model, key, raw)
            else:
                assert Decimal(str(round(raw * 1_000_000, 4))) == value, (model, key)


def test_conflicting_identity_is_refused(origin):
    run = Recorder(server.sdk(origin)).run(workload.WORKFLOW, "v9", "conflict")
    run.charge("answer", model="gpt-4.1-mini", usage=Usage(100, 10))
    with pytest.raises(httpx.HTTPStatusError) as refused:
        run.charge("answer", model="gpt-4.1-mini", usage=Usage(100, 11))
    assert refused.value.response.status_code == 422
    assert "conflicting immutable fields" in refused.value.response.text
