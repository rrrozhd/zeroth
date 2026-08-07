"""Assert that the coverage matrix actually happened — against the live API.

Reads the run ids ``runbook.py`` wrote to ``.runbook_state.json`` and checks,
through the public service API only:

* every expected node executed (audit records per node);
* all four execution-unit modes ran (inline / native / project / wrapped);
* the parallel fan-out produced one child subgraph run per dimension;
* the audit hash chain verifies for each run;
* evidence bundles export and count the approval;
* cost events reached the bundled econ plane for the tenant;
* the deployment attestation verifies.

Run (services still up):

    uv run python -m apps.vendor_dd.verify
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / ".runbook_state.json"

MAIN_URL = "http://127.0.0.1:8730"
API_KEY = "vendor-dd-ops-key"
DEPLOYMENT = "vendor-dd"

EXPECTED_MAIN_NODES = {
    "intake",
    "normalize",
    "policy-context",
    "screen",
    "financial-metrics",
    "prepare-panel",
    "risk-score",
    "report",
    "report-stamp",
}
UNIT_MODES = {"inline", "native", "project", "wrapped_command"}


def get(path: str, headers: dict | None = None) -> dict:
    request = Request(f"{MAIN_URL}{path}")
    request.add_header("X-API-Key", API_KEY)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urlopen(request, timeout=15) as resp:  # noqa: S310 - local service
        return json.loads(resp.read())


def post(path: str, payload: dict) -> dict:
    request = Request(f"{MAIN_URL}{path}", data=json.dumps(payload).encode(), method="POST")
    request.add_header("X-API-Key", API_KEY)
    request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=15) as resp:  # noqa: S310 - local service
        return json.loads(resp.read())


CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    if not STATE_FILE.exists():
        print("run the runbook first: uv run python -m apps.vendor_dd.runbook")
        return 1
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    clean_run = state["clean_run_id"]
    risky_run = state["risky_run_id"]
    tenant = state.get("tenant", "tenant-acme")

    print("── vendor-dd coverage verification ───────────────────────")

    # 1. Node coverage + unit modes, from the audit trail of the clean run.
    audits = get(f"/v1/deployments/{DEPLOYMENT}/audits?run_id={clean_run}")["records"]
    node_ids = {record["node_id"] for record in audits}
    missing = EXPECTED_MAIN_NODES - node_ids
    check(
        "all main-graph nodes audited",
        not missing,
        f"{len(node_ids)} node audits" + (f", missing {missing}" if missing else ""),
    )

    modes_seen = set()
    subgraph_children = set()
    tool_calls = 0
    for record in audits:
        meta = record.get("execution_metadata") or {}
        mode = meta.get("execution_mode")
        if mode:
            modes_seen.add(mode)
        if meta.get("subgraph_run_id"):
            subgraph_children.add(meta["subgraph_run_id"])
        tool_calls += len(record.get("tool_calls") or [])
        if meta.get("retrieval"):
            check(
                "retrieval grounded on policy corpus",
                meta["retrieval"].get("result_count", 0) > 0,
                f"{meta['retrieval'].get('result_count')} chunks for "
                f"query {meta['retrieval'].get('query', '')[:40]!r}",
            )
    check(
        "all four execution-unit modes ran",
        modes_seen >= UNIT_MODES,
        f"saw {sorted(modes_seen & UNIT_MODES)}",
    )
    check(
        "parallel fan-out spawned child subgraph runs",
        len(subgraph_children) >= 3,
        f"{len(subgraph_children)} child runs (financial/security/compliance)",
    )
    check("agent tool calls audited (tool edge)", tool_calls >= 1, f"{tool_calls} calls")

    # 2. Tenant attribution.
    tenants = {record.get("tenant_id") for record in audits}
    check("audit records are tenant-true", tenants == {tenant}, f"tenants={tenants}")

    # 3. Hash-chain verification for both business runs.
    for label, run_id in (("clean", clean_run), ("risky", risky_run)):
        verification = get(f"/v1/runs/{run_id}/audit-verification")
        check(
            f"audit chain verifies ({label} run)",
            bool(verification.get("verified")),
            f"{verification.get('record_count', '?')} records"
            + (f", error={verification.get('error')}" if verification.get("error") else ""),
        )

    # 4. Evidence bundle for the approval run.
    evidence = get(f"/v1/runs/{risky_run}/evidence")
    summary = evidence.get("summary", {})
    check(
        "evidence bundle exports with approval recorded",
        summary.get("approval_count", 0) >= 1 and summary.get("audit_count", 0) > 0,
        json.dumps(summary),
    )

    # 5. Cost events reached the bundled econ plane.
    from zeroth.econ.analytics.service_auth import mint_econ_service_token

    token = mint_econ_service_token()
    cost = get(f"/v1/tenants/{tenant}/cost", headers={"Authorization": f"Bearer {token}"})
    check(
        "tenant cost visible in econ plane",
        "total_cost_usd" in json.dumps(cost),
        json.dumps(cost)[:140],
    )

    # 6. Deployment attestation: export it, then hand it back for verification.
    attestation = get(f"/v1/deployments/{DEPLOYMENT}/attestation")
    result = post(f"/v1/deployments/{DEPLOYMENT}/verify-attestation", attestation)
    check(
        "deployment attestation verifies",
        bool(result.get("verified", result.get("valid", False))),
        json.dumps(result)[:120],
    )

    failed = [name for name, passed, _ in CHECKS if not passed]
    print(
        f"\n{'OK' if not failed else 'FAIL'} — "
        f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed"
        + (f"; failed: {failed}" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
