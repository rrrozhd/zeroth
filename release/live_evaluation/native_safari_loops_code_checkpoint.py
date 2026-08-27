"""Validate and seal native Safari loop architecture and persistence evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore

RUN_ID = "f3816ae65dec4f13a7eabca62df9956c"
WORKFLOW_ID = "da5da69b-1086-4cfe-8090-424a0118b88c"
DEPLOYMENT_REF = "demo-incident-readiness-loop-manifest-v1"
GRAPH_VERSION_REF = f"{WORKFLOW_ID}@2"

SCREENSHOTS = (
    "incident-loop-before-reload.jpeg",
    "incident-loop-inspector.jpeg",
    "incident-loop-after-reload.jpeg",
    "incident-limit-run.jpeg",
    "incident-limit-run-after-reload.jpeg",
)
ACCESSIBILITY = (
    "incident-loop-before-reload.txt",
    "incident-loop-inspector.txt",
    "incident-loop-after-reload.txt",
    "incident-limit-run.txt",
    "incident-limit-run-after-reload.txt",
)


def _require_all(text: str, *tokens: str) -> None:
    missing = [token for token in tokens if token not in text]
    if missing:
        raise RuntimeError(f"native Safari evidence is missing {missing!r}")


def build_checkpoint(*, source_root: Path, destination: Path) -> Path:
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)

    texts = {
        name: (source_root / "accessibility" / name).read_text()
        for name in ACCESSIBILITY
    }
    for name in ("incident-loop-before-reload.txt", "incident-loop-after-reload.txt"):
        _require_all(
            texts[name],
            "Incident readiness review — local manifests",
            "published v 2",
            "text REPEAT",
            "text DONE",
            "text LIMIT",
            "1 attempt + 2 retries",
            "e-retry-readiness.repeat-assess.input-data",
            "e-retry-readiness.done-finalize.input-data",
            "e-retry-readiness.limit-escalate.input-data",
        )
    _require_all(
        texts["incident-loop-inspector.txt"],
        "Done condition",
        "payload.ready == True",
        "Max retries",
        "Value: 2",
        "Exhaustion exits through Limit",
        "Read-only (published)",
    )
    for name in ("incident-limit-run.txt", "incident-limit-run-after-reload.txt"):
        _require_all(
            texts[name],
            RUN_ID,
            DEPLOYMENT_REF,
            GRAPH_VERSION_REF,
            "max_retries_exhausted",
            "assess 3 visits",
            "prepare 3 visits",
            "escalate 1 visit",
            "Routing decisions",
        )

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "native-safari-loops-and-code",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "workflow_id": WORKFLOW_ID,
            "deployment_ref": DEPLOYMENT_REF,
            "graph_version_ref": GRAPH_VERSION_REF,
            "run_id": RUN_ID,
            "browser": "native Safari through macOS accessibility automation",
            "provider_calls_performed": 0,
            "screenshot_count": len(SCREENSHOTS),
            "accessibility_snapshot_count": len(ACCESSIBILITY),
        }
    )

    evidence_paths: list[str] = []
    for name in SCREENSHOTS:
        source = source_root / "screenshots" / name
        if not source.is_file() or not source.read_bytes().startswith(b"\xff\xd8\xff"):
            raise RuntimeError(f"invalid native Safari screenshot: {name}")
        relative = Path("screenshots") / name
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())
    for name in ACCESSIBILITY:
        source = source_root / "accessibility" / name
        relative = Path("accessibility") / name
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())

    event_id = store.append_event(
        "campaign.native_safari.loops_code_verified",
        {
            "result": "pass",
            "workflow_id": WORKFLOW_ID,
            "deployment_ref": DEPLOYMENT_REF,
            "graph_version_ref": GRAPH_VERSION_REF,
            "repeat_route_visible": True,
            "done_route_visible": True,
            "limit_route_visible": True,
            "max_retries": 2,
            "limit_reason": "max_retries_exhausted",
            "refresh_restored": True,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(run_id=RUN_ID),
    )
    evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion("LOOPS-NATIVE-SAFARI-001", "pass", evidence),
            AcceptanceCriterion("LOOPS-ROUTES-VISIBLE-002", "pass", evidence),
            AcceptanceCriterion("LOOPS-RETRY-BOUND-003", "pass", evidence),
            AcceptanceCriterion("LOOPS-REFRESH-RESTORE-004", "pass", evidence),
            AcceptanceCriterion("RUNS-LIMIT-ROUTE-005", "pass", evidence),
        ),
        report_markdown=(
            "# Native Safari loops and code checkpoint\n\n"
            "Native Safari displayed the published incident-readiness workflow as a "
            "dedicated Loop node with explicit Repeat, Done, and Limit routes. The "
            "read-only inspector showed the done condition and two additional retries. "
            f"A real reload restored the same architecture. Safari then restored run "
            f"`{RUN_ID}` before and after another reload; its output recorded "
            "`max_retries_exhausted`, three body visits, one escalation visit, and the "
            "complete routing timeline. No provider call was made.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        source_root=args.source_root,
        destination=args.destination,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
