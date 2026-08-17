"""Certifier-owned process and result-channel boundary for candidate imports."""

from __future__ import annotations

import ast
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .models import AppDeclaration

_OUTPUT_LIMIT = 1 << 20
_CPU_LIMIT = 120
_MEMORY_LIMIT = 2 * 1024 * 1024 * 1024
_PROCESS_LIMIT = 128
_OPEN_FILE_LIMIT = 256
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FORBIDDEN_CALLS = frozenset(
    {
        "builtins.exit",
        "builtins.quit",
        "fcntl.fcntl",
        "os._exit",
        "os.abort",
        "os.dup",
        "os.dup2",
        "os.dup3",
        "os.kill",
        "os.killpg",
        "os.pwrite",
        "os.set_inheritable",
        "os.write",
        "os.writev",
        "signal.raise_signal",
        "sys.exit",
        "exit",
        "quit",
    }
)
_FORBIDDEN_METHODS = frozenset({"recvmsg", "sendmsg"})
_PROBE_BOOTSTRAP = (
    "import pathlib,runpy,sys;"
    "certifier=pathlib.Path(sys.argv.pop(1));"
    "venv=pathlib.Path(sys.argv.pop(1));"
    "site_packages=venv/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/"
    "'site-packages';"
    "sys.prefix=sys.exec_prefix=str(venv);"
    "sys.path[:0]=[str(certifier),str(certifier/'src'),str(site_packages)];"
    "runpy.run_module('release.app_certification.candidate_process',run_name='__main__')"
)


def _cap_resource(kind: int, limit: int) -> None:
    soft, hard = resource.getrlimit(kind)
    capped_hard = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
    capped_soft = capped_hard if soft == resource.RLIM_INFINITY else min(capped_hard, soft)
    resource.setrlimit(kind, (capped_soft, capped_hard))


def _limit_resources() -> None:
    limits = [
        (resource.RLIMIT_FSIZE, _OUTPUT_LIMIT),
        (resource.RLIMIT_CPU, _CPU_LIMIT),
        (resource.RLIMIT_NOFILE, _OPEN_FILE_LIMIT),
    ]
    if sys.platform != "darwin":
        limits.extend(
            (
                (resource.RLIMIT_AS, _MEMORY_LIMIT),
                (resource.RLIMIT_NPROC, _PROCESS_LIMIT),
            )
        )
    for kind, limit in limits:
        _cap_resource(kind, limit)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


def _wait_process(argv: list[str], *, stdout: int) -> tuple[int, str, bool]:
    with tempfile.TemporaryFile(mode="w+b") as stderr:
        process = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            text=False,
            preexec_fn=_limit_resources,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=150)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _terminate_process(process)
        stderr.seek(0)
        diagnostics = stderr.read(_OUTPUT_LIMIT + 1).decode(errors="replace")
    if len(diagnostics) > _OUTPUT_LIMIT:
        return 1, "candidate diagnostics exceeded limit", timed_out
    return process.returncode, diagnostics, timed_out


def run_importer(argv: list[str]) -> tuple[int, str, str]:
    """Run a bounded subprocess and retain its untrusted text output."""
    with tempfile.TemporaryFile() as stdout:
        returncode, diagnostics, timed_out = _wait_process(argv, stdout=stdout.fileno())
        stdout.seek(0)
        raw = stdout.read(_OUTPUT_LIMIT + 1)
    if timed_out:
        return 1, raw.decode(errors="replace"), diagnostics or "candidate serializer timed out"
    if len(raw) > _OUTPUT_LIMIT:
        return 1, "", "candidate output exceeded limit"
    return returncode, raw.decode(errors="replace"), diagnostics


def _qualified_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value, aliases)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _source_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                name = imported.asname or imported.name.split(".")[0]
                aliases[name] = imported.name if imported.asname else name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for imported in node.names:
                aliases[imported.asname or imported.name] = f"{node.module}.{imported.name}"
    return aliases


def _target_name(reference: str) -> str:
    attribute = reference.partition(":")[2]
    return (attribute or reference.rpartition(".")[2]).split(".")[-1]


def _is_main_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(value, ast.Constant) and value.value == "__main__"
            for value in node.test.comparators
        )
    )


def _policy_reaches(node: ast.AST, parents: dict[ast.AST, ast.AST], target: str) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if _is_main_guard(current) or isinstance(current, ast.Lambda):
            return False
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name == target
    return True


def _validate_source_policy(path: Path, reference: str) -> None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        raise ValueError(
            f"candidate target source is unreadable for {reference!r}: {error}"
        ) from error
    aliases = _source_aliases(tree)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    target = _target_name(reference)
    for node in ast.walk(tree):
        if not _policy_reaches(node, parents, target):
            continue
        if isinstance(node, ast.Call):
            name = _qualified_name(node.func, aliases)
            if name in _FORBIDDEN_CALLS or (name and name.rpartition(".")[2] in _FORBIDDEN_METHODS):
                raise ValueError(
                    f"candidate target {reference!r} uses forbidden process control {name!r}"
                )
        elif isinstance(node, ast.Raise):
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = _qualified_name(raised, aliases)
            if name in {"SystemExit", "builtins.SystemExit"}:
                raise ValueError(
                    f"candidate target {reference!r} raises forbidden process control {name!r}"
                )


def _source_path(root: Path, reference: str) -> Path:
    module_name = reference.partition(":")[0]
    if ":" not in reference:
        module_name = reference.rpartition(".")[0]
    module = Path(*module_name.split("."))
    source = next(
        (
            path
            for path in (root / module.with_suffix(".py"), root / module / "__init__.py")
            if path.is_file()
        ),
        None,
    )
    if source is None:
        raise ValueError(f"declared target source is missing for {reference!r}")
    source = source.resolve()
    source.relative_to(root.resolve())
    return source


def _validate_candidate_sources(
    name: str,
    root: Path,
    declaration: AppDeclaration,
    reference: str | None,
) -> None:
    from .checks import candidate_target_references

    references = [reference] if reference else candidate_target_references(name, declaration)
    for target in dict.fromkeys(references):
        _validate_source_policy(_source_path(root, target), target)


def _probe_prefix(user: str | None) -> list[str]:
    if user is None:
        return []
    if _USER.fullmatch(user) is None:
        raise ValueError("untrusted user must be a simple local account name")
    environment = [
        f"{name}={os.environ[name]}"
        for name in ("ZEROTH_DATABASE__BACKEND", "ZEROTH_DATABASE__POSTGRES_DSN")
        if name in os.environ
    ]
    return [
        "sudo",
        "--non-interactive",
        "--user",
        user,
        "--",
        "env",
        "-i",
        f"HOME=/home/{user}",
        "LANG=C.UTF-8",
        f"PATH={os.environ.get('PATH', '')}",
        *environment,
    ]


def probe_candidate(
    name: str,
    root: Path,
    declaration: AppDeclaration,
    candidate_venv: Path,
    *,
    reference: str | None = None,
    database_url: str | None = None,
    untrusted_user: str | None = None,
) -> Any:
    """Validate target source and return only bounded, untrusted candidate data."""
    _validate_candidate_sources(name, root, declaration, reference)
    inner = [
        str(Path(sys.executable).absolute()),
        "-I",
        "-S",
        "-c",
        _PROBE_BOOTSTRAP,
        str(Path(__file__).parents[2].resolve()),
        str(candidate_venv.resolve()),
        name,
        "--root",
        str(root),
        "--declaration-json",
        declaration.model_dump_json(),
    ]
    if reference is not None:
        inner.extend(("--reference", reference))
    if database_url is not None:
        inner.extend(("--database-url", database_url))
    returncode, raw, diagnostics = run_importer([*_probe_prefix(untrusted_user), *inner])
    if returncode:
        raise ValueError(diagnostics.strip() or raw.strip() or "candidate probe failed")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("candidate probe returned malformed provisional data") from error
