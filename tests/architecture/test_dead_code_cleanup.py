from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tree(path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))


def _top_level_functions(path: str) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _class_methods(path: str, class_name: str) -> set[str]:
    classes = {node.name: node for node in _tree(path).body if isinstance(node, ast.ClassDef)}
    return {
        node.name
        for node in classes[class_name].body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_dead_python_definitions_are_removed_without_pruning_live_paths() -> None:
    assert "_approval_visible_to_deployment" not in _top_level_functions(
        "src/zeroth/service/api/approval_api.py"
    )
    assert "nodes_in_any_loop" not in _top_level_functions(
        "src/zeroth/runtime/orchestration/token_scope.py"
    )
    assert "_continuation_inbound_edge" not in _class_methods(
        "src/zeroth/runtime/orchestration/token_runtime_support.py", "TokenRuntimeSupport"
    )
    assert not (REPO_ROOT / "scripts/check_token_engine.py").exists()

    assert "_enforce" in _top_level_functions("src/zeroth/integrations/langgraph/_tool_guard.py")
    store_methods = _class_methods(
        "src/zeroth/integrations/persistence/runs/run_repository.py", "_RunThreadStore"
    )
    repository_methods = _class_methods(
        "src/zeroth/integrations/persistence/runs/run_repository.py", "RunRepository"
    )
    assert {"delete_run", "list_dead_letter_runs"} <= store_methods
    assert {"delete", "list_dead_letter_runs"} <= repository_methods


def test_dead_frontend_declarations_are_removed_without_pruning_live_aliases() -> None:
    source = (REPO_ROOT / "frontend/app/lib/api.ts").read_text(encoding="utf-8")

    for name in (
        "getRunAuditVerification",
        "getDeploymentAttestation",
        "verifyDeploymentAttestation",
        "getCost",
    ):
        assert re.search(rf"\bfunction\s+{name}\b", source) is None

    assert "export type QualityEconomics" not in source
    assert "export type StudioContract" not in source
    assert re.search(r"\btype\s+WebhookDeadLetter\s*=", source) is None

    assert re.search(r"\bexport\s+async\s+function\s+apiFetch\b", source)
    assert "export const listWebhookDeadLetters = listDeadLetters;" in source
    assert "export const replayWebhookDeadLetter = replayDeadLetter;" in source
