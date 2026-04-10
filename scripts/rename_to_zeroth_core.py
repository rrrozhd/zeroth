#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

import libcst as cst
from libcst.helpers import get_full_name_for_node


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = ("src", "tests", "apps", "scripts")
TEXT_ROOT_FILES = (
    "pyproject.toml",
    "Dockerfile",
    "docker-compose.yml",
    "alembic.ini",
    "README.md",
    "zeroth.yaml",
)
TEXT_EXTENSIONS = (".md", ".toml", ".yaml", ".yml")
TEXT_ROOT_DIRS = ("docs",)
EXCLUDED_PYTHON_FILES = {Path("scripts/rename_to_zeroth_core.py")}
EXCLUDED_TEXT_PATH_PARTS = {".planning"}
EXCLUDED_ROOT_TEXT_FILES = {"PROGRESS.md"}
TOP_LEVEL_PACKAGES = (
    "agent_runtime",
    "approvals",
    "audit",
    "conditions",
    "config",
    "contracts",
    "demos",
    "deployments",
    "dispatch",
    "econ",
    "execution_units",
    "graph",
    "guardrails",
    "identity",
    "mappings",
    "memory",
    "migrations",
    "observability",
    "orchestrator",
    "policy",
    "runs",
    "sandbox_sidecar",
    "secrets",
    "service",
    "storage",
    "studio",
    "webhooks",
)
MODULE_PATTERN = "|".join(sorted((re.escape(name) for name in TOP_LEVEL_PACKAGES), key=len, reverse=True))
MODULE_PATH_RE = re.compile(
    rf"\bzeroth\.(?P<module>{MODULE_PATTERN})(?=(?:\.|\b))"
)


def rewrite_python_import_path(path: str) -> str:
    if path == "zeroth":
        return "zeroth.core"
    if path.startswith("zeroth.") and not path.startswith("zeroth.core."):
        return f"zeroth.core.{path.removeprefix('zeroth.')}"
    return path


def rewrite_module_path_literals(text: str) -> str:
    return MODULE_PATH_RE.sub(r"zeroth.core.\g<module>", text)


def rewrite_text_file_contents(path: Path, text: str) -> str:
    rewritten = text

    if path.name == "pyproject.toml":
        rewritten = rewritten.replace('name = "zeroth"', 'name = "zeroth-core"', 1)
        rewritten = rewritten.replace('packages = ["src/zeroth"]', 'packages = ["src/zeroth/core"]', 1)

    if path.name == "Dockerfile":
        rewritten = rewritten.replace(
            "python -m zeroth.service.entrypoint",
            "python -m zeroth.core.service.entrypoint",
        )

    if path.name == "alembic.ini":
        rewritten = rewritten.replace(
            "script_location = src/zeroth/migrations",
            "script_location = src/zeroth/core/migrations",
        )

    return rewrite_module_path_literals(rewritten)


def _parse_dotted_name(name: str) -> cst.BaseExpression:
    return cst.parse_expression(name)


class RenameTransformer(cst.CSTTransformer):
    def leave_Import(
        self, original_node: cst.Import, updated_node: cst.Import
    ) -> cst.Import:
        aliases: list[cst.ImportAlias] = []
        changed = False

        for alias in updated_node.names:
            full_name = get_full_name_for_node(alias.name)
            if full_name is None:
                aliases.append(alias)
                continue

            rewritten_name = rewrite_python_import_path(full_name)
            if rewritten_name != full_name:
                alias = alias.with_changes(name=_parse_dotted_name(rewritten_name))
                changed = True
            aliases.append(alias)

        if not changed:
            return updated_node
        return updated_node.with_changes(names=tuple(aliases))

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated_node.module is None:
            return updated_node

        full_name = get_full_name_for_node(updated_node.module)
        if full_name is None:
            return updated_node

        rewritten_name = rewrite_python_import_path(full_name)
        if rewritten_name == full_name:
            return updated_node

        return updated_node.with_changes(module=_parse_dotted_name(rewritten_name))

    def leave_SimpleString(
        self, original_node: cst.SimpleString, updated_node: cst.SimpleString
    ) -> cst.SimpleString:
        rewritten = rewrite_module_path_literals(updated_node.value)
        if rewritten == updated_node.value:
            return updated_node
        return updated_node.with_changes(value=rewritten)


def rewrite_python_source(source: str) -> str:
    module = cst.parse_module(source)
    rewritten = module.visit(RenameTransformer())
    return rewritten.code


def iter_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for root_name in PYTHON_ROOTS:
        base = root / root_name
        if not base.exists():
            continue
        files.extend(sorted(path for path in base.rglob("*.py") if path.is_file()))
    return files


def iter_text_files(root: Path) -> list[Path]:
    candidates: set[Path] = set()

    for file_name in TEXT_ROOT_FILES:
        path = root / file_name
        if path.exists() and path.is_file():
            candidates.add(path)

    for suffix in TEXT_EXTENSIONS:
        for path in root.glob(f"*{suffix}"):
            if path.is_file() and path.name not in EXCLUDED_ROOT_TEXT_FILES:
                candidates.add(path)

    for directory_name in TEXT_ROOT_DIRS:
        base = root / directory_name
        if not base.exists():
            continue
        for suffix in TEXT_EXTENSIONS:
            for path in base.rglob(f"*{suffix}"):
                if path.is_file():
                    candidates.add(path)

    return sorted(
        path
        for path in candidates
        if not any(part in EXCLUDED_TEXT_PATH_PARTS for part in path.relative_to(root).parts)
    )


def rewrite_file(path: Path, rewritten_text: str) -> bool:
    original_text = path.read_text(encoding="utf-8")
    if rewritten_text == original_text:
        return False
    path.write_text(rewritten_text, encoding="utf-8")
    return True


def apply_python_rewrites(root: Path) -> int:
    changed = 0
    for path in iter_python_files(root):
        relative_path = path.relative_to(root)
        if relative_path in EXCLUDED_PYTHON_FILES:
            continue
        original_text = path.read_text(encoding="utf-8")
        rewritten_text = rewrite_python_source(original_text)
        changed += int(rewrite_file(path, rewritten_text))
    return changed


def apply_text_rewrites(root: Path) -> int:
    changed = 0
    for path in iter_text_files(root):
        original_text = path.read_text(encoding="utf-8")
        rewritten_text = rewrite_text_file_contents(path, original_text)
        changed += int(rewrite_file(path, rewritten_text))
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rewrite Zeroth imports and text references from zeroth.* to zeroth.core.*."
    )
    parser.add_argument(
        "--rewrite-text",
        action="store_true",
        help="Rewrite non-Python metadata and text references instead of Python source files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.rewrite_text:
        changed = apply_text_rewrites(ROOT)
        print(f"rewrote {changed} text files")
        return 0

    changed = apply_python_rewrites(ROOT)
    print(f"rewrote {changed} python files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
