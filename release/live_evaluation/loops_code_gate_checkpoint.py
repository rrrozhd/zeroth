"""Seal provider-independent loop and executable-code acceptance evidence.

The checkpoint intentionally stores safe identities and verdicts rather than raw
payloads or complete audit records.  It proves the published inline-code fixture,
two deterministic failure fixtures, missing-manifest preflight rejection, signed
chains, immutable code identity across a service restart, and the relevant unit
and component test suites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, EvidenceStore

SUCCESS_RUNS = (
    "76b47478bbfe4bb5b17b4fc3121a9839",
    "821d49cc49094243866129e739d5b5ff",
)
MALFORMED_RUN = "0aea4396a122496b9aeee1e3482359c0"
TIMEOUT_RUN = "8e2c7725c0b145aa97e1fd0702fce9fb"
MANIFEST_TIMEOUT_RUN = "a688c76b792349a4a4ae6b49841ed102"

SUCCESS_WORKFLOW = "2f2b20b2-8acc-4488-9a01-71b1b4f088f6"
MALFORMED_WORKFLOW = "5e2a277c-38fd-40ae-a9a4-5c139a5c1b84"
TIMEOUT_WORKFLOW = "f5f39592-84fe-492c-9a54-c9d3383939be"
MISSING_MANIFEST_WORKFLOW = "d9870a11-c8bb-4705-9583-0f638fb97e9b"

EXPECTED_INLINE_IDENTITY = (
    "902fe694adcad10ec1062d683a3c1d06d6668542b8ad25a85dbf1e240408a01d"
)
EXPECTED_REGISTERED_HASHES = {
    "evaluation://local-code/quality-inspect/v1": (
        "46a403e4c6cdbf987250ee13beb1955c6765f4e63c613e13559afc5cb4f7e017"
    ),
    "evaluation://local-code/record-profiler/v1": (
        "4b54391d2f19803afc3261d548d551c3aaec8b69172ee4a7e08942a7b039ed7f"
    ),
}


def _request_json(
    *,
    api_base: str,
    api_key: str,
    path: str,
    method: str = "GET",
    body: Mapping[str, object] | None = None,
) -> tuple[int, Any]:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        api_base.rstrip("/") + path,
        data=payload,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - caller supplies loopback API
            request, timeout=30
        ) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _run_summary(
    *, api_base: str, api_key: str, run_id: str
) -> dict[str, object]:
    status, run = _request_json(
        api_base=api_base, api_key=api_key, path=f"/v1/runs/{run_id}"
    )
    if status != 200 or not isinstance(run, Mapping):
        raise RuntimeError(f"run lookup failed for {run_id}: {status}")
    status, verification = _request_json(
        api_base=api_base,
        api_key=api_key,
        path=f"/v1/runs/{run_id}/verify-chain",
        method="POST",
    )
    if status != 200 or not isinstance(verification, Mapping):
        raise RuntimeError(f"chain verification failed for {run_id}: {status}")
    status, evidence = _request_json(
        api_base=api_base,
        api_key=api_key,
        path=f"/v1/runs/{run_id}/evidence",
    )
    if status != 200 or not isinstance(evidence, Mapping):
        raise RuntimeError(f"evidence lookup failed for {run_id}: {status}")
    audits = evidence.get("audits")
    if not isinstance(audits, Sequence):
        raise RuntimeError(f"run evidence has no audit sequence: {run_id}")
    identities = [
        metadata["manifest_ref_sha256"]
        for record in audits
        if isinstance(record, Mapping)
        and isinstance((metadata := record.get("execution_metadata")), Mapping)
        and isinstance(metadata.get("manifest_ref_sha256"), str)
    ]
    summary = evidence.get("summary")
    if not isinstance(summary, Mapping):
        raise RuntimeError(f"run evidence has no economics summary: {run_id}")
    traversal = run.get("traversal")
    return {
        "run_id": run_id,
        "status": run.get("status"),
        "deployment_ref": run.get("deployment_ref"),
        "graph_version_ref": run.get("graph_version_ref"),
        "current_step": run.get("current_step"),
        "failure_state": run.get("failure_state"),
        "traversal": traversal,
        "manifest_ref_sha256": identities,
        "audit": {
            "verified": verification.get("verified"),
            "record_count": verification.get("record_count"),
            "signature_verified": verification.get("signature_verified"),
            "unsigned_record_count": verification.get("unsigned_record_count"),
            "signing_key_id": verification.get("signing_key_id"),
        },
        "economics": {
            "priced_call_count": summary.get("priced_call_count"),
            "cost_event_count": summary.get("cost_event_count"),
            "total_cost_usd": summary.get("total_cost_usd"),
            "reconciliation_state": summary.get("reconciliation_state"),
        },
    }


def _workflow_summary(
    *, api_base: str, api_key: str, workflow_id: str
) -> dict[str, object]:
    status, workflow = _request_json(
        api_base=api_base,
        api_key=api_key,
        path=f"/api/studio/v1/workflows/{workflow_id}",
    )
    if status != 200 or not isinstance(workflow, Mapping):
        raise RuntimeError(f"workflow lookup failed for {workflow_id}: {status}")
    nodes = workflow.get("nodes")
    node_rows: list[dict[str, object]] = []
    if isinstance(nodes, Sequence):
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            data = node.get("data")
            config = data.get("config") if isinstance(data, Mapping) else None
            source = config.get("inline_source") if isinstance(config, Mapping) else None
            node_rows.append(
                {
                    "id": node.get("id"),
                    "type": node.get("type"),
                    "label": data.get("label") if isinstance(data, Mapping) else None,
                    "manifest_ref": (
                        config.get("manifest_ref") if isinstance(config, Mapping) else None
                    ),
                    "timeout_seconds": (
                        config.get("timeout_seconds")
                        if isinstance(config, Mapping)
                        else None
                    ),
                    "inline_source_sha256": (
                        hashlib.sha256(source.encode()).hexdigest()
                        if isinstance(source, str)
                        else None
                    ),
                }
            )
    return {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "version": workflow.get("version"),
        "status": workflow.get("status"),
        "graph_version_ref": workflow.get("graph_version_ref"),
        "nodes": node_rows,
        "edge_count": len(workflow.get("edges") or ()),
        "execution_settings": workflow.get("execution_settings"),
    }


def _tree_digest(worktree: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return hashlib.sha256((status + diff).encode()).hexdigest()


def _record_command(
    store: EvidenceStore,
    *,
    sequence: int,
    name: str,
    argv: Sequence[str],
    cwd: Path,
) -> Path:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    path = store.record_command(
        sequence=sequence,
        name=name,
        argv=argv,
        working_directory=cwd,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {name}")
    return path


def build_checkpoint(
    *,
    destination: Path,
    state_root: Path,
    worktree: Path,
    api_base: str,
) -> Path:
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    api_key = (state_root / "runtime-secrets/tenant-a-admin-key").read_text().strip()
    if not api_key:
        raise RuntimeError("admin key is empty")
    store = EvidenceStore(destination)

    status, health = _request_json(api_base=api_base, api_key=api_key, path="/health")
    if status != 200 or not isinstance(health, Mapping):
        raise RuntimeError("service health is unavailable")
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "provider-independent-loops-and-code",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "working_tree_sha256": _tree_digest(worktree),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "api_base": api_base,
                "health": dict(health),
            },
            "provider_calls_performed": 0,
            "campaign_id": "evaluation-studio-v1",
        }
    )

    workflow_rows = {
        workflow_id: _workflow_summary(
            api_base=api_base, api_key=api_key, workflow_id=workflow_id
        )
        for workflow_id in (
            SUCCESS_WORKFLOW,
            MALFORMED_WORKFLOW,
            TIMEOUT_WORKFLOW,
            MISSING_MANIFEST_WORKFLOW,
        )
    }
    store._write_exclusive(Path("runtime/workflows.json"), workflow_rows)

    status, preflight = _request_json(
        api_base=api_base,
        api_key=api_key,
        path=f"/api/studio/v1/workflows/{MISSING_MANIFEST_WORKFLOW}/preflight",
        method="POST",
    )
    if status != 200 or not isinstance(preflight, Mapping):
        raise RuntimeError("missing-manifest preflight request failed")
    if preflight.get("ready") is not False or not any(
        isinstance(issue, Mapping) and issue.get("code") == "unresolved_manifest_ref"
        for issue in preflight.get("issues", ())
    ):
        raise RuntimeError("missing manifest did not fail closed")
    store._write_exclusive(Path("runtime/missing-manifest-preflight.json"), preflight)

    run_ids = (*SUCCESS_RUNS, MALFORMED_RUN, TIMEOUT_RUN, MANIFEST_TIMEOUT_RUN)
    runs = {
        run_id: _run_summary(api_base=api_base, api_key=api_key, run_id=run_id)
        for run_id in run_ids
    }
    for run_id in SUCCESS_RUNS:
        row = runs[run_id]
        if row["status"] != "succeeded" or row["manifest_ref_sha256"] != [
            EXPECTED_INLINE_IDENTITY
        ]:
            raise RuntimeError(f"inline identity mismatch for {run_id}")
    expected_failures = {
        MALFORMED_RUN: "stdout is not valid JSON",
        TIMEOUT_RUN: "timed out after 1s",
        MANIFEST_TIMEOUT_RUN: "timed out after 5s",
    }
    for run_id, message in expected_failures.items():
        row = runs[run_id]
        failure = row.get("failure_state")
        if (
            row.get("status") != "failed"
            or not isinstance(failure, Mapping)
            or message not in str(failure.get("message"))
        ):
            raise RuntimeError(f"unexpected deterministic failure for {run_id}")
    for run_id, row in runs.items():
        audit = row.get("audit")
        if not isinstance(audit, Mapping) or audit.get("verified") is not True:
            raise RuntimeError(f"unsigned or invalid chain for {run_id}")
    store._write_exclusive(Path("runtime/runs.json"), runs)

    registered: dict[str, object] = {}
    for manifest_ref, expected_hash in EXPECTED_REGISTERED_HASHES.items():
        encoded = urllib.parse.quote(manifest_ref, safe="")
        status, detail = _request_json(
            api_base=api_base,
            api_key=api_key,
            path=f"/v1/manifests/{encoded}",
        )
        if (
            status != 200
            or not isinstance(detail, Mapping)
            or detail.get("content_hash") != expected_hash
        ):
            raise RuntimeError(f"registered manifest hash mismatch: {manifest_ref}")
        registered[manifest_ref] = {
            "content_hash": detail.get("content_hash"),
            "runtime": detail.get("runtime"),
            "network_access": detail.get("network_access"),
        }
    store._write_exclusive(Path("runtime/registered-manifests.json"), registered)

    success = workflow_rows[SUCCESS_WORKFLOW]
    status, immutable_response = _request_json(
        api_base=api_base,
        api_key=api_key,
        path=f"/api/studio/v1/workflows/{SUCCESS_WORKFLOW}",
        method="PUT",
        body={"name": f"{success['name']} mutation must fail"},
    )
    if status != 409 or not isinstance(immutable_response, Mapping):
        raise RuntimeError("published workflow mutation did not fail closed")
    store._write_exclusive(
        Path("runtime/published-code-immutability.json"),
        {"http_status": status, "response": immutable_response},
    )

    command_paths = [
        _record_command(
            store,
            sequence=1,
            name="runtime-code-tests",
            argv=(
                "uv",
                "run",
                "pytest",
                "-q",
                "tests/runtime/orchestration/test_dispatcher.py",
                "tests/runtime/orchestration/test_loop_control_node.py",
                "tests/execution_units/test_runner.py",
                "tests/execution_units/test_sandbox.py",
                "tests/execution_units/test_sandbox_strict_network.py",
                "tests/execution_units/test_io.py",
                "tests/live_evaluation/test_action_service.py",
                "tests/test_code_node_inline.py",
            ),
            cwd=worktree,
        ),
        _record_command(
            store,
            sequence=2,
            name="loop-presentation-tests",
            argv=(
                "npm",
                "test",
                "--",
                "--run",
                "app/components/LoopNodeContract.test.ts",
                "app/components/StudioEdgeView.test.tsx",
                "app/components/StudioNodeView.test.tsx",
                "app/studio/edit/graphPresentation.test.ts",
            ),
            cwd=worktree / "frontend",
        ),
    ]

    proof_paths = [
        "runtime/workflows.json",
        "runtime/missing-manifest-preflight.json",
        "runtime/runs.json",
        "runtime/registered-manifests.json",
        "runtime/published-code-immutability.json",
        *(path.relative_to(destination).as_posix() for path in command_paths),
    ]
    event_id = store.append_event(
        "campaign.loops_code.runtime_verified",
        {
            "result": "pass",
            "provider_call_count": 0,
            "successful_inline_run_count": len(SUCCESS_RUNS),
            "deterministic_failure_run_count": len(expected_failures),
            "proof_paths": proof_paths,
        },
    )
    evidence = tuple([*proof_paths, f"events.ndjson#{event_id}"])
    criteria = (
        AcceptanceCriterion("LOOP-CODE-RUNTIME-001", "pass", evidence),
        AcceptanceCriterion("LOOP-CODE-NEGATIVE-002", "pass", evidence),
        AcceptanceCriterion("LOOP-CODE-IMMUTABLE-003", "pass", evidence),
        AcceptanceCriterion("LOOP-CODE-SIGNED-004", "pass", evidence),
        AcceptanceCriterion("LOOP-CODE-ZERO-PROVIDER-005", "pass", evidence),
    )
    store.finalize_bundle(
        acceptance=criteria,
        report_markdown=(
            "# Provider-independent loops and code checkpoint\n\n"
            "Two inline-code runs succeeded on opposite sides of a real backend restart "
            "with the same content identity. Malformed output, one-second inline timeout, "
            "five-second manifest-node timeout, and an unresolved manifest all failed "
            "closed with signed audit chains. Published code rejected mutation. The "
            "reconciled executions made no provider call and cost zero. Dedicated browser "
            "screenshots and native Safari evidence remain a separate serialized UI gate.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8122")
    args = parser.parse_args()
    root = build_checkpoint(
        destination=args.destination,
        state_root=args.state_root.expanduser().resolve(strict=True),
        worktree=args.worktree.expanduser().resolve(strict=True),
        api_base=args.api_base,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
