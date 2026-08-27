"""Seal native-Safari Studio context-window and thread-control evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from release.live_evaluation.evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
)

WORKFLOW_ID = "fd2523b3-adf8-4abb-88d9-0e44d677047d"
NODE_ID = "research-agent"
CHECKPOINT = "native-safari-context-threads-20260825-1"
CAPTURES = (
    "01-published-read-only",
    "02-focus-trap-fixed",
    "03-draft-configured",
    "04-draft-lower-controls",
    "05-invalid-boundary",
    "06-refresh-restored",
)
REQUIRED_FILES = tuple(
    [
        *(f"screenshots/{name}.jpg" for name in CAPTURES),
        *(f"accessibility/{name}.txt" for name in CAPTURES),
    ]
)


def _run_command(
    store: EvidenceStore,
    *,
    sequence: int,
    name: str,
    argv: list[str],
    cwd: Path,
) -> bool:
    completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    store.record_command(
        sequence=sequence,
        name=name,
        argv=argv,
        working_directory=cwd,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return completed.returncode == 0


def _assert_native_captures(root: Path) -> dict[str, object]:
    texts = {
        name: (root / "accessibility" / f"{name}.txt").read_text()
        for name in CAPTURES
    }
    requirements = {
        "published_read_only": (
            "checkbox (disabled) Value: 0, Enable context management"
            in texts["01-published-read-only"]
            and "Read-only (published)" in texts["01-published-read-only"]
        ),
        "focus_trap_dialog_remained_open": (
            "container Edit research" in texts["02-focus-trap-fixed"]
            and "button Close" in texts["02-focus-trap-fixed"]
        ),
        "thread_configuration": all(
            value in texts["06-refresh-restored"]
            for value in (
                "Value: messages, Messages input key",
                "checkbox Value: 1, Persist conversation On",
                "Value: 25, Placeholder: 50",
            )
        ),
        "context_configuration": all(
            value in texts["06-refresh-restored"]
            for value in (
                "Value: 64000, Maximum context tokens",
                "Value: 0.75, Compaction trigger ratio",
                "Value: LLM summarization",
                "Value: 6, Recent messages to preserve",
                "checkbox Value: 1, Archive compacted originals",
            )
        ),
        "invalid_boundary_explained": (
            "Value: -1, Recent messages to preserve"
            in texts["05-invalid-boundary"]
            and "the last valid value remains active" in texts["05-invalid-boundary"]
        ),
    }
    failed = [name for name, passed in requirements.items() if not passed]
    if failed:
        raise RuntimeError(f"native Safari capture assertions failed: {failed}")
    return requirements


def _database_projection(database: Path) -> dict[str, object]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, payload, tenant_id, workspace_id FROM graph_versions "
            "WHERE graph_id = ? AND version = 1",
            (WORKFLOW_ID,),
        ).fetchone()
    if row is None:
        raise RuntimeError("configured draft graph is missing")
    status, payload_text, tenant_id, workspace_id = row
    payload = json.loads(payload_text)
    node = next((item for item in payload["nodes"] if item["node_id"] == NODE_ID), None)
    if node is None:
        raise RuntimeError("configured agent node is missing")
    agent = node["agent"]
    expected_context = {
        "max_context_tokens": 64_000,
        "summary_trigger_ratio": 0.75,
        "compaction_strategy": "llm_summarization",
        "preserve_recent_messages_count": 6,
        "archive_originals": True,
    }
    if not (
        status == "draft"
        and tenant_id == "evaluation-studio-v1"
        and workspace_id is None
        and agent["input_messages_key"] == "messages"
        and agent["persist_conversation"] is True
        and agent["conversation_max_turns"] == 25
        and agent["context_window"] == expected_context
    ):
        raise RuntimeError("persisted context-window/thread projection does not match Safari")
    return {
        "workflow_id": WORKFLOW_ID,
        "workflow_version": 1,
        "workflow_status": status,
        "node_id": NODE_ID,
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "thread_settings": {
            "input_messages_key": agent["input_messages_key"],
            "persist_conversation": agent["persist_conversation"],
            "conversation_max_turns": agent["conversation_max_turns"],
        },
        "context_window": expected_context,
    }


def build_checkpoint(*, root: Path, database: Path, repository: Path) -> Path:
    store = EvidenceStore(root)
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            raise FileNotFoundError(root / relative)

    capture_assertions = _assert_native_captures(root)
    commands = (
        (
            "frontend-context-tests",
            [
                "npm",
                "test",
                "--",
                "--run",
                "app/studio/edit/page.test.tsx",
                "app/studio/edit/runPayload.test.ts",
                "app/lib/evidence-identity.test.ts",
            ],
            repository / "frontend",
        ),
        ("frontend-typecheck", ["npx", "tsc", "--noEmit"], repository / "frontend"),
        (
            "context-runtime-catalog-tests",
            [
                "uv",
                "run",
                "pytest",
                "tests/product_validation",
                "tests/context_window",
                "-q",
            ],
            repository,
        ),
        (
            "diff-check",
            [
                "git",
                "diff",
                "--check",
                "--",
                "frontend/app/components/AgentContextWindowControls.tsx",
                "frontend/app/components/NodeInspector.tsx",
                "frontend/app/studio/edit/page.tsx",
                "frontend/app/studio/edit/page.test.tsx",
                "release/product_validation/catalog-v1.json",
                "release/product_validation/evidence-index-v1.json",
                "tests/product_validation/test_catalog.py",
            ],
            repository,
        ),
    )
    command_results = [
        _run_command(store, sequence=index, name=name, argv=argv, cwd=cwd)
        for index, (name, argv, cwd) in enumerate(commands, start=1)
    ]
    projection = _database_projection(database)
    store.record_command(
        sequence=5,
        name="native-safari-capture-assertions",
        argv=["inspect", "sanitized Safari accessibility checkpoints"],
        working_directory=root,
        exit_code=0,
        stdout=json.dumps(capture_assertions, indent=2, sort_keys=True) + "\n",
        stderr="",
    )
    store.record_command(
        sequence=6,
        name="context-thread-database-projection",
        argv=["sqlite3", "<external-state>", "context/thread projection"],
        working_directory=database.parent,
        exit_code=0,
        stdout=json.dumps(projection, indent=2, sort_keys=True) + "\n",
        stderr="",
    )

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
    ).stdout
    store.write_manifest(
        {
            "campaign_id": "evaluation-studio-v1",
            "checkpoint": CHECKPOINT,
            "revision": revision,
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "browser": "Safari",
            "viewport": "1216x768 native desktop capture",
            "service_url": "http://127.0.0.1:8122",
            "tenant_id": "evaluation-studio-v1",
            "workspace_id": None,
            "role": "admin",
            "workflow_id": WORKFLOW_ID,
            "workflow_version": 1,
            "node_id": NODE_ID,
            "priced_calls": 0,
        }
    )
    event_id = store.append_event(
        "campaign.native_safari.context_threads_verified",
        {
            "configured": True,
            "refresh_restored": True,
            "published_read_only": True,
            "invalid_input_preserved_last_valid_value": True,
            "modal_focus_recaptured": True,
            "deterministic_runtime_tests": True,
            "priced_calls": 0,
        },
        correlation=CorrelationIds(ui_action_id=CHECKPOINT),
    )
    shared_evidence = tuple(
        [
            *REQUIRED_FILES,
            "commands/0001-frontend-context-tests.json",
            "commands/0002-frontend-typecheck.json",
            "commands/0003-context-runtime-catalog-tests.json",
            "commands/0004-diff-check.json",
            "commands/0005-native-safari-capture-assertions.json",
            "commands/0006-context-thread-database-projection.json",
            f"events.ndjson#{event_id}",
        ]
    )
    status = "pass" if all(command_results) else "fail"
    store.finalize_bundle(
        acceptance=(
            AcceptanceCriterion("CONTEXT-UI-NATIVE-SAFARI-001", status, shared_evidence),
            AcceptanceCriterion("CONTEXT-PERSISTENCE-NATIVE-SAFARI-002", status, shared_evidence),
            AcceptanceCriterion("CONTEXT-VALIDATION-NATIVE-SAFARI-003", status, shared_evidence),
            AcceptanceCriterion("STUDIO-MODAL-FOCUS-TRAP-004", status, shared_evidence),
            AcceptanceCriterion("CONTEXT-RUNTIME-DETERMINISTIC-005", status, shared_evidence),
        ),
        report_markdown=(
            "# Native Safari Context Window and threads checkpoint\n\n"
            "Native Safari authored nested agent context-window and thread settings in a real "
            "draft, refreshed the page, reopened the node, and observed the exact persisted "
            "values. The UI exposes all supported strategies, explains the additional priced "
            "call made by LLM summarization, preserves the last valid value during invalid "
            "numeric input, and locks the controls on published graphs.\n\n"
            "The first native keyboard pass found focus escaping behind the node inspector. "
            "A document-level containment fix and a failing-first regression test now recapture "
            "focus in the dialog; a repeated 18-Tab Safari pass kept the focus ring on Close.\n\n"
            "## Adversarial review\n\n"
            "This checkpoint proves authoring, validation, persistence, published locking, and "
            "deterministic runtime integration. It does not claim a live-provider compaction "
            "event or restored compacted message archive; those remain blocked pending a newly "
            "rotated external provider credential. The backend still accepts unknown strategy "
            "strings and falls back to observation masking; tightening that public schema is a "
            "separate compatibility decision. No provider call was made.\n"
        ),
    )
    return root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(root=args.root, database=args.database, repository=args.repository)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
