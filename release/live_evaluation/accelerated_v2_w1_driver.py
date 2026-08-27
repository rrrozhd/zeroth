"""One-shot Workflow 1 driver for the accelerated V2 acceptance profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import httpx

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .live_tool_retrieval import EXPECTED_SOURCE_ID, EXPECTED_TOOL_RECEIPT

PHRASE = "AUTHORIZE_ACCELERATED_DEMO_ACCEPTANCE_V2"
PROFILE_ID = "evaluation-studio-v1-accelerated-demo-v2"
GATE_ID = "accelerated-v2.workflow1.live-grounded-run"
DEPLOYMENT = "live-tool-retrieval-w1-tool-20260826-1"
GRAPH = "5f49dc4e-26c2-4f53-a93a-46e7e396adfc@1"
TENANT = "evaluation-studio-v1"
QUERY = "What is the approved queue depth?"
TERMINAL = {
    "succeeded",
    "failed",
    "terminated_by_policy",
    "terminated_by_loop_guard",
    "dead_letter",
}


def _git(workdir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workdir, check=True, capture_output=True, text=True
    ).stdout.strip()


def _request(client: httpx.Client, method: str, path: str, body: object | None = None) -> object:
    response = client.request(method, path, json=body)
    response.raise_for_status()
    return response.json()


def _decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise RuntimeError("non-finite economics value")
    return result


def _main(args: argparse.Namespace) -> int:
    profile_raw = args.profile.resolve(strict=True).read_bytes()
    profile = json.loads(profile_raw)
    gate = next((row for row in profile["gates"] if row["gate_id"] == GATE_ID), None)
    if (
        args.authorize != PHRASE
        or profile.get("profile_id") != PROFILE_ID
        or profile.get("status") != "proposed_unarmed"
        or profile.get("authorization_phrase") != PHRASE
        or not isinstance(gate, dict)
        or gate.get("maximum_new_live_runs") != 1
        or gate.get("maximum_provider_calls") != 3
    ):
        raise RuntimeError("V2 profile or execution interlock is invalid")
    if args.evidence_root.exists():
        raise FileExistsError(args.evidence_root)

    credential = args.service_auth_file.resolve(strict=True).read_text().strip()
    if not credential:
        raise RuntimeError("service credential source is empty")
    headers = {"X-API-Key": credential, "X-Tenant-ID": TENANT, "Accept": "application/json"}
    try:
        with httpx.Client(base_url=args.base_url, headers=headers, timeout=10.0) as client:
            health = _request(client, "GET", "/health")
            ready = _request(client, "GET", "/health/ready")
            before = _request(client, "GET", f"/v1/tenants/{TENANT}/cost")
            if not isinstance(health, dict) or (
                health.get("deployment_ref") != DEPLOYMENT
                or health.get("graph_version_ref") != GRAPH
            ):
                raise RuntimeError("exact W1 serving identity is not active")
            if not isinstance(ready, dict) or ready.get("status") != "ok":
                raise RuntimeError("service readiness failed")
            if not isinstance(before, dict) or any(
                _decimal(before.get(name)) != 0
                for name in ("active_exposure_usd", "ambiguous_exposure_usd")
            ):
                raise RuntimeError("provider reservation preflight failed")

            submitted = _request(
                client,
                "POST",
                "/v1/runs",
                {"input_payload": {"query": QUERY}, "campaign_id": TENANT},
            )
            if not isinstance(submitted, dict) or not isinstance(submitted.get("run_id"), str):
                raise RuntimeError("run submission did not return an identity")
            run_id = submitted["run_id"]
            run = submitted
            for _ in range(240):
                run = _request(client, "GET", f"/v1/runs/{quote(run_id, safe='')}")
                if isinstance(run, dict) and run.get("status") in TERMINAL:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError("W1 run did not reach a terminal state")
            evidence = _request(client, "GET", f"/v1/runs/{quote(run_id, safe='')}/evidence")
            timeline = _request(client, "GET", f"/v1/runs/{quote(run_id, safe='')}/timeline")
            verification = _request(
                client, "POST", f"/v1/runs/{quote(run_id, safe='')}/verify-chain", {}
            )
            after = _request(client, "GET", f"/v1/tenants/{TENANT}/cost")
    finally:
        credential = ""
        headers.clear()

    if not all(isinstance(value, dict) for value in (run, evidence, timeline, verification, after)):
        raise RuntimeError("W1 evidence response is malformed")
    audits = evidence.get("audits")
    summary = evidence.get("summary")
    output = run.get("terminal_output")
    if not isinstance(audits, list) or not isinstance(summary, dict):
        raise RuntimeError("W1 audit evidence is missing")
    audit_rows = [row for row in audits if isinstance(row, dict)]
    cost_ids = sorted({str(row["cost_event_id"]) for row in audit_rows if row.get("cost_event_id")})
    retrieval_rows = [row for row in audit_rows if row.get("node_id") == "retrieve"]
    retrieval_count = None
    if retrieval_rows:
        metadata = retrieval_rows[-1].get("execution_metadata")
        if isinstance(metadata, dict):
            retrieval_count = metadata.get("retrieval_result_count")
    spend_delta = _decimal(after.get("budget_consumed_usd")) - _decimal(
        before.get("budget_consumed_usd")
    )
    output_ok = (
        isinstance(output, dict)
        and EXPECTED_SOURCE_ID in output.get("source_ids", [])
        and output.get("tool_receipt") == EXPECTED_TOOL_RECEIPT
        and isinstance(output.get("answer"), str)
        and "four" in output["answer"].lower()
    )
    passed = (
        run.get("status") == "succeeded"
        and output_ok
        and isinstance(retrieval_count, int)
        and retrieval_count > 0
        and summary.get("tool_call_count") == 1
        and 1 <= summary.get("priced_call_count", 0) <= 3
        and len(cost_ids) == summary.get("cost_event_count")
        and verification.get("verified") is True
        and verification.get("signature_verified") is True
        and _decimal(summary.get("total_cost_usd")) == spend_delta
        and Decimal("0") < spend_delta <= Decimal("0.25")
        and _decimal(after.get("active_exposure_usd")) == 0
        and _decimal(after.get("ambiguous_exposure_usd")) == 0
    )

    store = EvidenceStore(args.evidence_root)
    revision = _git(args.workdir, "rev-parse", "HEAD")
    diff = _git(args.workdir, "diff", "--binary", "HEAD")
    store.write_manifest(
        {
            "schema_version": 1,
            "profile_id": PROFILE_ID,
            "gate_id": GATE_ID,
            "revision": revision,
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "profile_sha256": hashlib.sha256(profile_raw).hexdigest(),
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "tenant_id": TENANT,
            "execution_authorized": True,
            "maximum_provider_calls": 3,
        }
    )
    event_id = store.append_event(
        "accelerated_v2.w1.run.completed",
        {
            "status": run.get("status"),
            "retrieval_result_count": retrieval_count,
            "priced_call_count": summary.get("priced_call_count"),
            "tool_call_count": summary.get("tool_call_count"),
            "cost_usd": format(spend_delta, "f"),
            "chain_verified": verification.get("verified"),
            "signature_verified": verification.get("signature_verified"),
        },
        correlation=CorrelationIds(run_id=run_id),
    )
    sanitized_audits = [
        {
            "audit_id": row.get("audit_id"),
            "run_id": row.get("run_id"),
            "node_id": row.get("node_id"),
            "status": row.get("status"),
            "chain_sequence": row.get("chain_sequence"),
            "record_digest": row.get("record_digest"),
            "previous_record_digest": row.get("previous_record_digest"),
            "record_signature": row.get("record_signature"),
            "signing_key_id": row.get("signing_key_id"),
            "cost_event_id": row.get("cost_event_id"),
            "cost_measurement": row.get("cost_measurement"),
            "cost_usd": row.get("cost_usd"),
            "estimated_cost_usd": row.get("estimated_cost_usd"),
            "usage": row.get("token_usage"),
            "tool_calls": row.get("tool_calls", []),
            "memory_interactions": row.get("memory_interactions", []),
            "retrieval_result_count": (
                row.get("execution_metadata", {}).get("retrieval_result_count")
                if isinstance(row.get("execution_metadata"), dict)
                else None
            ),
        }
        for row in audit_rows
    ]
    observation = {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "gate_id": GATE_ID,
        "run": {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "deployment_ref": run.get("deployment_ref"),
            "graph_version_ref": run.get("graph_version_ref"),
            "tenant_id": run.get("tenant_id"),
            "campaign_id": run.get("campaign_id"),
            "terminal_output": output,
            "failure_state": run.get("failure_state"),
            "audit_refs": run.get("audit_refs", []),
        },
        "timeline": {
            "deployment_ref": timeline.get("deployment_ref"),
            "run_id": timeline.get("run_id"),
            "audit_ids": [row.get("audit_id") for row in audit_rows],
        },
        "evidence": {"summary": summary, "audits": sanitized_audits},
        "verification": verification,
        "economics_before": before,
        "economics_after": after,
        "incremental_cost_usd": format(spend_delta, "f"),
        "cost_event_ids": cost_ids,
        "retrieval_result_count": retrieval_count,
    }
    observation_path = args.observation
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    observation_path.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n")
    store.ingest_artifact(observation_path, "reconciliation/observation.json")
    result = "pass" if passed else "fail"
    note = (
        None
        if passed
        else f"terminal status={run.get('status')!r}; gate invariants did not all hold"
    )
    criteria = [
        AcceptanceCriterion(
            GATE_ID,
            result,
            (f"events.ndjson#{event_id}", "reconciliation/observation.json"),
            note,
        )
    ]
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Accelerated V2 Workflow 1\n\n"
            f"Status: **{result}**. Run `{run_id}` finished as `{run.get('status')}` with "
            f"{summary.get('priced_call_count')} priced calls, one tool call, and incremental "
            f"cost `{format(spend_delta, 'f')}` USD. Audit continuity and keyed signatures "
            f"verified: `{verification.get('verified')}` / "
            f"`{verification.get('signature_verified')}`.\n"
        ),
    )
    print(
        json.dumps(
            {
                "gate": GATE_ID,
                "status": result,
                "run_id": run_id,
                "cost_usd": str(spend_delta),
            }
        )
    )
    return 0 if passed else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--service-auth-file", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8122")
    parser.add_argument("--authorize", required=True)
    return _main(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
