"""Reject actionable documentation references that no longer exist."""

from __future__ import annotations

import ast
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import get_args

REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = REPO_ROOT / "scripts/docs_reference_allowlist.txt"
BASELINE = REPO_ROOT / "scripts/docs_reference_seed.txt"

FROM_IMPORT_RE = re.compile(
    r"(?<![\w.])from\s+(?P<module>zeroth(?:\.[A-Za-z_]\w*)*)\s+import\s*\(?"
    r"(?P<members>[^)#;]+)"
)
IMPORT_RE = re.compile(r"(?<![\w.])import\s+(zeroth(?:\.[A-Za-z_]\w*)*)")
ENV_RE = re.compile(r"\bZEROTH_[A-Z0-9_*{}]+")
SOURCE_PATH_RE = re.compile(r"(?<![/\w])(src/[A-Za-z0-9_./-]+(?::\d+(?:-\d+)?)?)")
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
INLINE_CONTEXT_BOUNDARY_RE = re.compile(r"(?:[;.!?]\s+|,\s+(?:and|but|while)\s+)", re.I)
PIP_INSTALL_RE = re.compile(r"(?:^|\s)(?:[\w./-]*/)?pip\s+install\s+(.+)")
HISTORICAL_CONTEXT_RE = re.compile(
    r"\b(before|historical|no longer|obsolete|previously|removed|retired|used to)\b",
    re.I,
)


@dataclass(frozen=True, order=True)
class Violation:
    """One repository-relative broken actionable reference."""

    path: str
    line: int
    kind: str
    target: str

    @property
    def key(self) -> str:
        return f"{self.path}:{self.line}:{self.kind}:{self.target}"


def _model_type(annotation: object) -> type | None:
    from pydantic import BaseModel

    candidates = (annotation, *get_args(annotation))
    return next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, type) and issubclass(candidate, BaseModel)
        ),
        None,
    )


@lru_cache
def valid_environment_variables(repo_root: Path) -> set[str]:
    """Derive supported nested settings and explicit runtime variables."""
    sys.path.insert(0, str(repo_root / "src"))
    try:
        from zeroth.platform.config.settings import ZerothSettings
    finally:
        sys.path.pop(0)

    names: set[str] = set()

    def add_model(model: type, prefix: str = "ZEROTH_") -> None:
        for name, field in model.model_fields.items():
            override = (field.json_schema_extra or {}).get("env")
            nested = _model_type(field.annotation)
            if override:
                names.add(str(override))
            elif nested:
                names.add(f"{prefix}{name.upper()}")
                add_model(nested, f"{prefix}{name.upper()}__")
            else:
                names.add(f"{prefix}{name.upper()}")

    add_model(ZerothSettings)
    for path in sorted((repo_root / "src" / "zeroth").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments = {
            target.id: node.value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        }
        returns = {
            node.name: returned.value
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for returned in ast.walk(node)
            if isinstance(returned, ast.Return) and returned.value is not None
        }

        def value(
            node: ast.expr,
            seen: frozenset[str] = frozenset(),
            assignments: dict[str, ast.expr] = assignments,
            returns: dict[str, ast.expr] = returns,
        ) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                if node.id in seen:
                    return None
                return (
                    value(assignments[node.id], seen | {node.id})
                    if node.id in assignments
                    else None
                )
            if isinstance(node, ast.JoinedStr):
                return "".join(
                    part.value
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else "{}"
                    for part in node.values
                )
            if isinstance(node, ast.Call):
                function_name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                if function_name in returns:
                    return value(returns[function_name], seen)
            return None

        def is_environment_mapping(
            node: ast.expr,
            seen: frozenset[str] = frozenset(),
            assignments: dict[str, ast.expr] = assignments,
        ) -> bool:
            if isinstance(node, ast.Name):
                return (
                    node.id.endswith("env")
                    or "environment" in node.id
                    or node.id not in seen
                    and node.id in assignments
                    and is_environment_mapping(assignments[node.id], seen | {node.id})
                )
            if isinstance(node, ast.Attribute):
                return (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr == "environ"
                    or node.attr.endswith("env")
                    or "environment" in node.attr
                )
            if isinstance(node, (ast.BoolOp, ast.IfExp)):
                values = node.values if isinstance(node, ast.BoolOp) else (node.body, node.orelse)
                return any(is_environment_mapping(value, seen) for value in values)
            if isinstance(node, ast.Call):
                return any(is_environment_mapping(argument, seen) for argument in node.args)
            return False

        for node in ast.walk(tree):
            candidate = None
            if isinstance(node, ast.Call) and node.args:
                function = node.func
                if (
                    isinstance(function, ast.Attribute)
                    and function.attr in {"get", "getenv", "setdefault"}
                    and (
                        isinstance(function.value, ast.Name)
                        and function.value.id == "os"
                        or is_environment_mapping(function.value)
                    )
                ):
                    candidate = value(node.args[0])
            elif isinstance(node, ast.Subscript):
                target = node.value
                if is_environment_mapping(target):
                    candidate = value(node.slice)
            if candidate:
                names.update(ENV_RE.findall(candidate))
    return names


def _module_exists(module: str, repo_root: Path) -> bool:
    relative = Path(*module.split("."))
    source = repo_root / "src" / relative
    return _path_exists(source, repo_root) or _path_exists(source.with_suffix(".py"), repo_root)


def _path_exists(path: Path, repo_root: Path) -> bool:
    """Check path components exactly even on case-insensitive filesystems."""
    try:
        parts = path.relative_to(repo_root).parts
    except ValueError:
        return False
    current = repo_root
    for part in parts:
        if not current.is_dir() or part not in {child.name for child in current.iterdir()}:
            return False
        current /= part
    return True


@lru_cache
def _module_members(module: str, repo_root: Path) -> frozenset[str]:
    """Read public module bindings without importing optional dependencies."""
    source = repo_root / "src" / Path(*module.split("."))
    path = source / "__init__.py" if source.is_dir() else source.with_suffix(".py")
    if not path.is_file():
        return frozenset()

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    members: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            members.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                members.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            members.update(target.id for target in targets if isinstance(target, ast.Name))
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, (ast.AnnAssign, ast.AugAssign))
            else []
        )
        names = {target.id for target in targets if isinstance(target, ast.Name)}
        if "__all__" in names:
            members.update(
                item.value
                for item in ast.walk(node.value)
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value.isidentifier()
            )
        if "_EXPORTS" in names and isinstance(node.value, ast.Dict):
            members.update(
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value.isidentifier()
            )
    return frozenset(members)


def _member_exists(module: str, member: str, repo_root: Path) -> bool:
    return (
        member == "*"
        or _module_exists(f"{module}.{member}", repo_root)
        or member in _module_members(module, repo_root)
    )


def _environment_exists(name: str, valid: set[str]) -> bool:
    if "*" in name or "{" in name:
        prefix = name.split("*", 1)[0].split("{", 1)[0]
        return any(candidate.startswith(prefix) for candidate in valid)
    if name.endswith("_"):
        return any(candidate.startswith(name) for candidate in valid)
    return name in valid


def _source_exists(target: str, repo_root: Path) -> bool:
    path = re.sub(r":\d+(?:-\d+)?$", "", target).rstrip(".,;:)")
    return _path_exists(repo_root / path, repo_root)


def _install_violations(
    text: str,
    path: str,
    line: int,
    project: dict[str, object],
) -> list[Violation]:
    match = PIP_INSTALL_RE.search(text)
    if not match:
        return []
    try:
        arguments = shlex.split(match.group(1), comments=True)
    except ValueError:
        return []

    project_name = str(project["name"])
    version = str(project["version"])
    extras = set(project.get("optional-dependencies", {}))
    violations = []
    for argument in arguments:
        if argument.startswith("-") or argument in {".", ".."}:
            continue
        requirement = re.fullmatch(
            r"(?P<name>[A-Za-z0-9_.-]+)(?:\[(?P<extras>[^]]+)\])?(?P<specifier>.*)",
            argument,
        )
        if not requirement or not requirement["name"].lower().startswith("zeroth"):
            continue
        supplied_name = requirement["name"].lower().replace("_", "-")
        supplied_extras = set(filter(None, (requirement["extras"] or "").split(",")))
        versions = re.findall(r"\d+(?:\.\d+)+", requirement["specifier"])
        if (
            supplied_name != project_name
            or not supplied_extras <= extras
            or any(supplied != version for supplied in versions)
        ):
            violations.append(Violation(path, line, "install-target", argument))
    return violations


def _scan_actionable_text(
    text: str,
    path: str,
    line: int,
    repo_root: Path,
    valid_env: set[str],
    project: dict[str, object],
) -> list[Violation]:
    violations = []
    for match in FROM_IMPORT_RE.finditer(text):
        module = match.group("module")
        if not _module_exists(module, repo_root):
            violations.append(Violation(path, line, "import", module))
            continue
        members = match.group("members").split("#", 1)[0]
        for imported in members.split(","):
            member = imported.strip().split()[0] if imported.strip() else ""
            if member and not _member_exists(module, member, repo_root):
                violations.append(Violation(path, line, "import-member", f"{module}.{member}"))
    for match in IMPORT_RE.finditer(text):
        module = match.group(1)
        if not _module_exists(module, repo_root):
            violations.append(Violation(path, line, "import", module))
    for name in ENV_RE.findall(text):
        if not _environment_exists(name, valid_env):
            violations.append(Violation(path, line, "environment", name))
    for target in SOURCE_PATH_RE.findall(text):
        if not _source_exists(target, repo_root):
            violations.append(Violation(path, line, "source-path", target))
    violations.extend(_install_violations(text, path, line, project))
    return violations


def _historical_inline_context(line: str, start: int, end: int) -> bool:
    """Suppress only the inline span nearest a historical marker."""
    context_start = 0
    context_end = len(line)
    for boundary in INLINE_CONTEXT_BOUNDARY_RE.finditer(line):
        if boundary.end() <= start:
            context_start = boundary.end()
        elif boundary.start() >= end:
            context_end = boundary.start()
            break
    before = line[context_start:start]
    historical_before = list(HISTORICAL_CONTEXT_RE.finditer(before))
    if historical_before and not INLINE_CODE_RE.search(before, historical_before[-1].end()):
        return True
    after = line[end:context_end]
    historical_after = HISTORICAL_CONTEXT_RE.search(after)
    return bool(historical_after and not INLINE_CODE_RE.search(after[: historical_after.start()]))


def scan_markdown(text: str, path: str, repo_root: Path = REPO_ROOT) -> list[Violation]:
    """Return broken references from code blocks and inline code only."""
    project = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    valid_env = valid_environment_variables(repo_root)
    violations: set[Violation] = set()
    in_fence = False
    fence_actionable = True
    previous_nonempty = ""
    continued_line = 0
    continued_text = ""

    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
                continued_line = 0
                continued_text = ""
            else:
                in_fence = True
                fence_actionable = not HISTORICAL_CONTEXT_RE.search(previous_nonempty)
            continue

        if in_fence:
            if re.match(r"\s*#\s*before\b", line, re.I):
                fence_actionable = False
            elif re.match(r"\s*#\s*after\b", line, re.I):
                fence_actionable = True
            elif fence_actionable:
                scan_line = f"{continued_text} {stripped}" if continued_text else line
                scan_line_number = continued_line or line_number
                continuing_import = bool(
                    continued_text
                    or re.search(r"\bfrom\s+zeroth(?:\.[A-Za-z_]\w*)*\s+import\s*\(", scan_line)
                )
                if scan_line.rstrip().endswith("\\") or (
                    continuing_import and scan_line.count("(") > scan_line.count(")")
                ):
                    continued_line = scan_line_number
                    continued_text = scan_line.rstrip().removesuffix("\\")
                    continue
                violations.update(
                    _scan_actionable_text(
                        scan_line,
                        path,
                        scan_line_number,
                        repo_root,
                        valid_env,
                        project,
                    )
                )
                continued_line = 0
                continued_text = ""
        else:
            for inline in INLINE_CODE_RE.finditer(line):
                if not _historical_inline_context(line, inline.start(), inline.end()):
                    violations.update(
                        _scan_actionable_text(
                            inline.group(1), path, line_number, repo_root, valid_env, project
                        )
                    )
            if stripped:
                previous_nonempty = stripped

    return sorted(violations)


def document_paths(repo_root: Path = REPO_ROOT) -> list[Path]:
    paths = list((repo_root / "docs").rglob("*.md"))
    for pattern in ("README*", "CONTRIBUTING*", "SECURITY*"):
        paths.extend(path for path in repo_root.glob(pattern) if path.is_file())
    return sorted(set(paths))


def scan_repository(repo_root: Path = REPO_ROOT) -> list[Violation]:
    violations = []
    for path in document_paths(repo_root):
        relative = path.relative_to(repo_root).as_posix()
        violations.extend(scan_markdown(path.read_text(encoding="utf-8"), relative, repo_root))
    return sorted(violations)


def load_allowlist(path: Path = ALLOWLIST) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def unexpected_violations(violations: list[Violation], allowlist: set[str]) -> list[Violation]:
    return [violation for violation in violations if violation.key not in allowlist]


def invalid_allowlist_entries(allowlist: set[str], baseline: set[str]) -> set[str]:
    """Return allowlist entries not present in the reviewed base inventory."""
    return allowlist - baseline


def main() -> int:
    violations = scan_repository()
    allowlist = load_allowlist(ALLOWLIST)
    invalid = invalid_allowlist_entries(allowlist, load_allowlist(BASELINE))
    unexpected = unexpected_violations(violations, allowlist - invalid)
    stale = sorted(allowlist - {violation.key for violation in violations})
    for violation in unexpected:
        print(violation.key)
    for key in sorted(invalid):
        print(f"allowlist entry absent from baseline: {key}")
    for key in stale:
        print(f"stale allowlist entry: {key}")
    return bool(unexpected or invalid or stale)


if __name__ == "__main__":
    raise SystemExit(main())
