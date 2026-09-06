"""Real SDK/HTTP history exposes exact portable calculation amounts."""

import json
import shutil
import subprocess
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from tests.econ_plane.test_charge_cost_revisions import client as revision_http_client
from tests.econ_plane.test_sdk_evidence_namespace import (
    NOW, definition, engine as database_engine, execution, outcome, scoped, user,
)
from zeroth.econ.charge_costs import ChargeCostRevision
from zeroth.econ.analytics.service_auth import mint_econ_service_token
from zeroth.econ.plane.cloud.api import record_execution, record_outcome
from zeroth.econ.plane.decisioning.api import router as decision_router
from zeroth.econ.plane.instrumentation.charge_costs import ingest_revision
from zeroth.protocol import VersionComparisonRequest as SdkComparison
from zeroth.sdk import ZerothClient

engine = database_engine
client = revision_http_client


def test_sdk_http_history_and_node_preserve_exact_portable_costs(engine, client):
    client.app.include_router(decision_router, prefix="/v1")
    with Session(engine) as raw:
        db = scoped(raw)
        for version, amount in [("v1", "9999999999.12345678"), ("v2", "0.00000001")]:
            definition(db, "invoice", version)
            record_execution(execution(version=version, event_id=version, cost_role="charge",
                                       charge_id=version, cost_usd="0"), db, user())
            record_outcome(outcome(version=version), db, user())
            ingest_revision(db, ChargeCostRevision(
                charge_id=version, asserted_at=NOW + timedelta(minutes=1),
                token_cost_usd=amount, cost_measurement="measured", reason="exact source charge",
            ))

    def forward(request):
        return client.request(request.method, request.url.path, params=request.url.params,
                              headers=dict(request.headers), content=request.read())

    with httpx.Client(transport=httpx.MockTransport(forward)) as transport:
        sdk = ZerothClient(api_key=mint_econ_service_token(), base_url="http://testserver", http_client=transport)
        report = sdk.compare_versions(SdkComparison(
            workflow="invoice", baseline_version="v1", candidate_version="v2",
            policy={"min_runs": 1, "min_success_rate": .5},
        ))
        assert sdk.list_decisions() == [report]
    assert report["calculation_inputs"]["baseline"][0]["cost_usd"] == "9999999999.12345678"
    assert report["calculation_inputs"]["candidate"][0]["cost_usd"] == "1E-8"
    node = shutil.which("node")
    assert node is not None, "Node is required for portable money acceptance"
    result = subprocess.run([node, "--input-type=module", "-e", r"""
import assert from 'node:assert/strict';
let input = ''; for await (const chunk of process.stdin) input += chunk;
const report = JSON.parse(input);
function units(value) {
  assert.equal(typeof value, 'string');
  const match = /^(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value);
  assert.ok(match);
  const [, whole, fraction = '', exponent = '0'] = match;
  const coefficient = BigInt(whole + fraction);
  const scale = 8 + Number(exponent) - fraction.length;
  if (scale >= 0) return coefficient * 10n ** BigInt(scale);
  const divisor = 10n ** BigInt(-scale);
  assert.equal(coefficient % divisor, 0n);
  return coefficient / divisor;
}
for (const [side, expected] of [['baseline', 999999999912345678n], ['candidate', 1n]]) {
  const rows = report.calculation_inputs[side];
  const total = rows.reduce((sum, row) => sum + units(row.cost_usd) * BigInt(row.runs), 0n);
  assert.equal(total, expected);
  assert.equal(total, units(report[side].measured_cost_usd));
}
process.stdout.write('exact portable USD totals verified');
"""], input=json.dumps(report), text=True, capture_output=True, check=True)
    assert result.stdout == "exact portable USD totals verified"
