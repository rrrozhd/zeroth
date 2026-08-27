"""Execute and seal the two provider-free Workflow-1 Chroma negatives."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import chromadb
import httpx

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .provider_free_composed import HttpFixtureClient
from .workflow1_deterministic_retrieval_live import CRITERIA, provision_fixture
from .workflow1_excessive_revision_live import DockerComposeServingController

EXPECTED_D012 = {
    "status": "ok",
    "campaign_id": "evaluation-studio-v1",
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "deployment_version": 1,
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}


def _json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    return value


def _browser_summary(root: Path, results: Mapping[str, Any]) -> dict[str, Any]:
    rows = results.get("artifacts")
    if not isinstance(rows, list):
        raise RuntimeError("browser evidence lacks artifacts")
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("destination"), str)
        and str(row["destination"]).endswith("workflow1-deterministic-retrieval-summary.json")
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
        raise RuntimeError("browser evidence lacks one deterministic retrieval summary")
    relative = Path(str(matches[0]["source"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("unsafe browser summary path")
    return _json((root / relative).resolve(strict=True), label="browser summary")


def _validate_summary(summary: Mapping[str, Any], *, deployment: str, graph: str) -> list[str]:
    health = summary.get("health")
    runs = summary.get("runs")
    if (
        summary.get("schema_version") != 1
        or not isinstance(health, Mapping)
        or health.get("deployment_ref") != deployment
        or health.get("graph_version_ref") != graph
        or summary.get("provider_calls_performed") != 0
        or summary.get("provider_request_ids") != []
        or summary.get("cost_event_ids") != []
        or summary.get("total_cost_usd") != 0
        or not isinstance(runs, list)
        or len(runs) != 2
    ):
        raise RuntimeError("deterministic retrieval summary identity/economics are invalid")
    expected = {
        "no_result": {
            "source_ids": [],
            "retrieval_result_count": 0,
            "answer": "No grounded result found in the tenant-scoped corpus.",
        },
        "conflict": {
            "source_ids": ["approved-queue-depth-four", "obsolete-queue-depth-six"],
            "retrieval_result_count": 2,
            "answer": "Conflict detected: approved and obsolete documents disagree.",
        },
    }
    run_ids: list[str] = []
    for row in runs:
        if not isinstance(row, Mapping):
            raise RuntimeError("deterministic retrieval run is malformed")
        terminal = row.get("terminal_output")
        chain = row.get("chain")
        run_id = row.get("run_id")
        if not isinstance(terminal, Mapping) or not isinstance(chain, Mapping):
            raise RuntimeError("deterministic retrieval run lacks output or chain")
        scenario = terminal.get("scenario")
        if scenario not in expected or not isinstance(run_id, str) or not run_id:
            raise RuntimeError("deterministic retrieval scenario identity is invalid")
        if (
            row.get("status") != "succeeded"
            or row.get("deployment_ref") != deployment
            or row.get("graph_version_ref") != graph
            or terminal.get("source_ids") != expected[scenario]["source_ids"]
            or terminal.get("answer") != expected[scenario]["answer"]
            or row.get("retrieval_result_count") != expected[scenario]["retrieval_result_count"]
            or chain.get("verified") is not True
            or chain.get("signature_verified") is not True
            or chain.get("unsigned_record_count") != 0
        ):
            raise RuntimeError(f"deterministic retrieval {scenario} result is invalid")
        run_ids.append(run_id)
    if len(set(run_ids)) != 2 or set(summary.get("refresh_restored_run_ids", [])) != set(run_ids):
        raise RuntimeError("deterministic retrieval refresh identities are invalid")
    return run_ids


def _safe_artifacts(root: Path, results: Mapping[str, Any]) -> list[tuple[Path, Path]]:
    rows = results.get("artifacts")
    if not isinstance(rows, list):
        raise RuntimeError("browser artifacts are missing")
    artifacts: list[tuple[Path, Path]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RuntimeError("browser artifact row is invalid")
        source = Path(str(row.get("source", "")))
        destination = Path(str(row.get("destination", "")))
        if (
            source.is_absolute()
            or destination.is_absolute()
            or ".." in source.parts
            or ".." in destination.parts
            or len(destination.parts) < 2
        ):
            raise RuntimeError("browser artifact path is unsafe")
        candidate = (root / source).resolve(strict=True)
        if root.resolve() not in candidate.parents or candidate.is_symlink():
            raise RuntimeError("browser artifact escaped its root")
        artifacts.append((candidate, destination))
    if sum(destination.parts[0] == "screenshots" for _, destination in artifacts) != 6:
        raise RuntimeError("deterministic retrieval proof requires exactly six screenshots")
    if sum(destination.parts[0] == "videos" for _, destination in artifacts) != 1:
        raise RuntimeError("deterministic retrieval proof requires exactly one video")
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    args = parser.parse_args(argv)
    workspace = Path(__file__).resolve().parents[2]
    frontend = workspace / "frontend"
    source_root = args.source_root.expanduser().resolve(strict=False)
    destination = args.destination.expanduser().resolve(strict=False)
    if source_root.exists() or destination.exists():
        raise FileExistsError("source and destination roots must be append-only new paths")
    source_store = EvidenceStore(source_root)
    api_key = args.api_key_file.expanduser().resolve(strict=True).read_text().strip()
    tenant = "evaluation-studio-v1"
    api_base = "http://127.0.0.1:8122"
    browser_root = source_root / "browser"
    client = httpx.Client(timeout=10.0, follow_redirects=False)
    request = HttpFixtureClient(
        base_url=api_base,
        api_key=api_key,
        tenant_id=tenant,
        client=client,
    )
    controller = DockerComposeServingController(
        request=request,
        workspace=workspace,
        compose_file=workspace / "compose.dev.yml",
    )
    before = controller.capture()
    if asdict(before) != EXPECTED_D012:
        raise RuntimeError("shared deployment window did not start at exact D012")
    fixture = None
    completed: subprocess.CompletedProcess[str] | None = None
    restore: dict[str, object] | None = None
    try:
        chroma_client = chromadb.HttpClient(host="127.0.0.1", port=8121)
        fixture = provision_fixture(
            request=request,
            chroma_client=chroma_client,
            tenant_id=tenant,
            fixture_id=args.fixture_id,
        )
        source_store._write_exclusive(Path("runtime/fixture.json"), asdict(fixture))
        controller.serve(
            deployment_ref=fixture.deployment_ref,
            deployment_version=fixture.deployment_version,
            graph_version_ref=fixture.graph_version_ref,
        )
        env = os.environ.copy()
        env.update(
            {
                "PLAYWRIGHT_NO_SERVER": "1",
                "ZEROTH_EVALUATION_W1_DETERMINISTIC_RETRIEVAL": "1",
                "ZEROTH_EVALUATION_API_BASE": api_base,
                "ZEROTH_EVALUATION_BASE_URL": "http://127.0.0.1:3000",
                "ZEROTH_EVALUATION_BROWSER_ROOT": str(browser_root),
                "ZEROTH_EVALUATION_TENANT": tenant,
                "ZEROTH_EVALUATION_API_KEY": api_key,
                "ZEROTH_EVALUATION_W1_RETRIEVAL_WORKFLOW_ID": fixture.workflow_id,
                "ZEROTH_EVALUATION_W1_RETRIEVAL_DEPLOYMENT_REF": fixture.deployment_ref,
                "ZEROTH_EVALUATION_W1_RETRIEVAL_GRAPH_VERSION": fixture.graph_version_ref,
                "ZEROTH_EVALUATION_W1_EMPTY_CONNECTOR": fixture.empty_connector_ref,
                "ZEROTH_EVALUATION_W1_CONFLICT_CONNECTOR": fixture.conflict_connector_ref,
            }
        )
        command = (
            "npm",
            "exec",
            "--",
            "playwright",
            "test",
            "e2e/workflow1-deterministic-retrieval-live.spec.ts",
            "--project=desktop-1440",
        )
        completed = subprocess.run(
            command,
            cwd=frontend,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        source_store.record_command(
            sequence=1,
            name="workflow1-deterministic-retrieval-playwright",
            argv=command,
            working_directory=frontend,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    finally:
        restore = controller.restore(before)
        client.close()
    if completed is None or completed.returncode != 0 or fixture is None:
        raise RuntimeError("deterministic retrieval Playwright journey failed")
    if restore is None or restore.get("exact") is not True:
        raise RuntimeError("exact D012 restoration was not proven")
    results = _json(browser_root / "results.json", label="browser results")
    criteria = results.get("criteria")
    if not isinstance(criteria, list) or {
        row.get("criterion_id"): row.get("status") for row in criteria if isinstance(row, Mapping)
    } != {criterion: "pass" for criterion in CRITERIA}:
        raise RuntimeError("browser criteria are not exact passes")
    summary = _browser_summary(browser_root, results)
    run_ids = _validate_summary(
        summary,
        deployment=fixture.deployment_ref,
        graph=fixture.graph_version_ref,
    )
    post_restart = []
    with httpx.Client(timeout=10.0) as post_client:
        for run_id in run_ids:
            response = post_client.get(
                f"{api_base}/v1/runs/{run_id}",
                headers={"X-API-Key": api_key, "X-Tenant-ID": tenant},
            )
            if response.status_code != 200:
                raise RuntimeError("run did not survive exact D012 restart")
            row = response.json()
            post_restart.append(
                {
                    "run_id": row.get("run_id"),
                    "status": row.get("status"),
                    "terminal_output": row.get("terminal_output"),
                }
            )
    summary["d012_restore"] = restore
    summary["post_restart_persistence"] = post_restart
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "workflow1-provider-free-local-chroma-negatives",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": tenant,
            "deployment_ref": fixture.deployment_ref,
            "graph_version_ref": fixture.graph_version_ref,
            "run_ids": run_ids,
            "accepted_criteria": list(CRITERIA),
            "provider_calls_performed": 0,
            "total_cost_usd": 0.0,
            "d012_restored": True,
        }
    )
    store._write_exclusive(Path("runtime/summary.json"), summary)
    store._write_exclusive(Path("runtime/fixture.json"), asdict(fixture))
    store.ingest_artifact(
        source_root / "commands/0001-workflow1-deterministic-retrieval-playwright.json",
        "handoff/commands/playwright.json",
    )
    evidence = [
        "manifest.json",
        "runtime/summary.json",
        "runtime/fixture.json",
        "handoff/commands/playwright.json",
    ]
    for source, relative in _safe_artifacts(browser_root, results):
        store.ingest_artifact(source, relative)
        evidence.append(relative.as_posix())
    event_id = store.append_event(
        "campaign.workflow1.deterministic_retrieval_negatives.verified",
        {
            "criteria": list(CRITERIA),
            "retrieval_result_counts": [0, 2],
            "signed": True,
            "provider_call_count": 0,
            "cost_event_count": 0,
            "total_cost_usd": 0.0,
            "post_restart_persistence": True,
            "d012_restored": True,
        },
        correlation=CorrelationIds(run_id=run_ids[0]),
    )
    references = tuple([*evidence, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", references) for criterion in CRITERIA
        ),
        report_markdown=(
            "# Workflow 1 deterministic local-Chroma negatives\n\n"
            "The real Studio UI submitted an empty-corpus query and an exact two-document "
            "conflict query through tenant-qualified Chroma connectors. Deterministic local "
            "reasoning abstained on zero results and reported both conflicting source IDs. "
            "Both signed chains verify, provider and cost identities are absent, total cost is "
            "`$0.00`, both runs survived the backend restart, and exact D-012 was restored. "
            "This proves the provider-free negative behavior only; it does not prove live-model "
            "semantic retrieval quality.\n"
        ),
    )
    print(destination)
    print("D012_RESTORED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
