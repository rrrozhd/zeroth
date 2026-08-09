#!/usr/bin/env python3
"""Find credential canaries without echoing credential material.

The scanner deliberately reports only where a value was observed, which rule
matched it, and a one-way fingerprint.  It is suitable for release evidence:
the JSON is deterministic and every required-input failure closes the gate.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

_CHUNK_SIZE = 64 * 1024
_FINGERPRINT_PREFIX = "sha256:"
_GITHUB_PATTERNS = (
    ("github:classic", re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,255}")),
    ("github:fine-grained", re.compile(rb"github_pat_[A-Za-z0-9_]{82,255}")),
)


def _fingerprint(value: bytes) -> str:
    return _FINGERPRINT_PREFIX + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Finding:
    """Leak-safe description of a match."""

    fingerprint: str
    rule: str
    surface: str

    def as_dict(self) -> dict[str, str]:
        return {
            "fingerprint": self.fingerprint,
            "rule": self.rule,
            "surface": self.surface,
        }


class ScanInputError(ValueError):
    """A required scanner input could not safely be consumed."""

    def __init__(self, rule: str, value: str) -> None:
        self.diagnostic = Finding(
            fingerprint=_fingerprint(value.encode("utf-8", errors="replace")),
            rule=rule,
            surface="required-input",
        )
        super().__init__("required scanner input rejected")


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ScanInputError("input:arguments-invalid", "command-line-arguments")


def _lower_percent_escapes(value: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), value)


def _ascii_case_pattern(value: bytes) -> bytes:
    parts: list[bytes] = []
    for item in value:
        character = bytes([item])
        if 65 <= item <= 70 or 97 <= item <= 102:
            lower = character.lower()
            parts.append(b"[" + lower + lower.upper() + b"]")
        else:
            parts.append(re.escape(character))
    return b"".join(parts)


def _percent_case_pattern(value: str) -> bytes:
    encoded = value.encode()
    parts: list[bytes] = []
    position = 0
    while position < len(encoded):
        if encoded[position : position + 1] == b"%" and position + 2 < len(encoded):
            parts.append(b"%" + _ascii_case_pattern(encoded[position + 1 : position + 3]))
            position += 3
        else:
            parts.append(re.escape(encoded[position : position + 1]))
            position += 1
    return b"".join(parts)


def _json_slash_pattern(value: bytes) -> bytes:
    return b"(?:/|\\\\/)".join(re.escape(part) for part in value.split(b"/"))


class CredentialLeakScanner:
    """Scan byte and structured observable surfaces for credentials."""

    def __init__(self, canaries: Iterable[str | bytes]) -> None:
        needles: list[tuple[str, bytes, str]] = []
        patterns: list[tuple[str, re.Pattern[bytes], str, bytes | None]] = []
        seen_variants: set[tuple[bytes, str]] = set()
        for raw_canary in canaries:
            canary = raw_canary if isinstance(raw_canary, bytes) else raw_canary.encode()
            if not canary:
                raise ValueError("credential canaries must not be empty")
            fingerprint = _fingerprint(canary)
            standard_base64 = base64.b64encode(canary)
            urlsafe_base64 = base64.urlsafe_b64encode(canary)
            variants = {
                "canary:exact": {canary},
                "canary:base64": {
                    standard_base64,
                    standard_base64.rstrip(b"="),
                    urlsafe_base64,
                    urlsafe_base64.rstrip(b"="),
                },
                "canary:hex": {canary.hex().encode(), canary.hex().upper().encode()},
            }
            patterns.append(
                (
                    "canary:hex",
                    re.compile(_ascii_case_pattern(canary.hex().encode())),
                    fingerprint,
                    None,
                )
            )
            try:
                text = canary.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                quoted = quote(text, safe="")
                plus_quoted = quote_plus(text, safe="")
                variants["canary:url"] = {
                    quoted.encode(),
                    plus_quoted.encode(),
                    _lower_percent_escapes(quoted).encode(),
                    _lower_percent_escapes(plus_quoted).encode(),
                }
                variants["canary:json"] = {
                    json.dumps(text, ensure_ascii=True)[1:-1].encode()
                }
                patterns.extend(
                    (
                        "canary:url",
                        re.compile(_percent_case_pattern(encoded_url)),
                        fingerprint,
                        None,
                    )
                    for encoded_url in (quoted, plus_quoted)
                    if encoded_url.encode() != canary
                )
                json_inner = json.dumps(text, ensure_ascii=True)[1:-1].encode()
                if json_inner != canary or b"/" in json_inner:
                    patterns.append(
                        (
                            "canary:json",
                            re.compile(_json_slash_pattern(json_inner)),
                            fingerprint,
                            b"\\/" if b"/" in json_inner else None,
                        )
                    )
            for rule, encoded_values in variants.items():
                for value in encoded_values:
                    identity = (value, fingerprint)
                    if value and identity not in seen_variants:
                        needles.append((rule, value, fingerprint))
                        seen_variants.add(identity)
        self._needles = tuple(needles)
        self._patterns = tuple(patterns)
        self._overlap = max(
            [300, *(len(needle) - 1 for _, needle, _ in self._needles)], default=300
        )

    def scan_bytes(self, value: bytes, *, surface: str) -> list[Finding]:
        safe_surface = self._safe_surface(surface)
        findings = {
            Finding(fingerprint=fingerprint, rule=rule, surface=safe_surface)
            for rule, needle, fingerprint in self._needles
            if needle in value
        }
        findings.update(
            Finding(fingerprint=fingerprint, rule=rule, surface=safe_surface)
            for rule, pattern, fingerprint, required in self._patterns
            if (match := pattern.search(value))
            and (required is None or required in match.group(0))
        )
        for rule, pattern in _GITHUB_PATTERNS:
            findings.update(
                Finding(
                    fingerprint=_fingerprint(match.group(0)), rule=rule, surface=safe_surface
                )
                for match in pattern.finditer(value)
            )
        return _sorted(findings)

    def _safe_surface(self, surface: str) -> str:
        encoded = surface.encode("utf-8", errors="replace")
        contains_canary = any(needle in encoded for _, needle, _ in self._needles)
        contains_encoded_canary = any(
            (match := pattern.search(encoded))
            and (required is None or required in match.group(0))
            for _, pattern, _, required in self._patterns
        )
        contains_github_token = any(pattern.search(encoded) for _, pattern in _GITHUB_PATTERNS)
        if contains_canary or contains_encoded_canary or contains_github_token:
            return "surface:" + _fingerprint(encoded)
        return surface

    def scan(self, value: Any, *, surface: str) -> list[Finding]:
        """Recursively inspect a structured value without stringifying it."""
        findings: set[Finding] = set()
        pending = [value]
        seen: set[int] = set()
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                findings.update(self.scan_bytes(item.encode(), surface=surface))
            elif isinstance(item, (bytes, bytearray, memoryview)):
                findings.update(self.scan_bytes(bytes(item), surface=surface))
            elif isinstance(item, Mapping):
                identity = id(item)
                if identity in seen:
                    continue
                seen.add(identity)
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set, frozenset)):
                identity = id(item)
                if identity in seen:
                    continue
                seen.add(identity)
                pending.extend(item)
        return _sorted(findings)

    def scan_file(self, path: Path, *, surface: str) -> list[Finding]:
        """Scan a file incrementally, preserving matches across chunk edges."""
        findings: set[Finding] = set()
        tail = b""
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                descriptor = None
                raise ScanInputError("input:not-file", str(path))
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                while chunk := handle.read(_CHUNK_SIZE):
                    window = tail + chunk
                    findings.update(self.scan_bytes(window, surface=surface))
                    tail = window[-self._overlap :]
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise ScanInputError("input:unreadable", str(path)) from error
        return _sorted(findings)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _validated_root(root: Path, *, rule_prefix: str) -> Path:
    if ".." in root.parts:
        raise ScanInputError(f"{rule_prefix}:root-parent-traversal", str(root))
    candidate = root if root.is_absolute() else Path.cwd() / root
    current = Path(candidate.anchor)
    try:
        status = current.lstat()
        for part in candidate.parts[1:]:
            current = current / part
            status = current.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise ScanInputError(f"{rule_prefix}:root-symlink", str(current))
    except ScanInputError:
        raise
    except OSError as error:
        raise ScanInputError(f"{rule_prefix}:root-unreadable", str(current)) from error
    if not stat.S_ISDIR(status.st_mode):
        raise ScanInputError(f"{rule_prefix}:root-not-directory", str(root))
    return current


def _contained(root: Path, path: Path) -> Path:
    if ".." in path.parts:
        raise ScanInputError("input:parent-traversal", str(path))
    candidate = _absolute(path)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise ScanInputError("input:outside-root", str(path)) from None
    current = root
    try:
        root_status = root.lstat()
        if stat.S_ISLNK(root_status.st_mode):
            raise ScanInputError("input:symlink", str(root))
        for part in relative.parts:
            current = current / part
            status = current.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise ScanInputError("input:symlink", str(current))
    except ScanInputError:
        raise
    except OSError as error:
        raise ScanInputError("input:unreadable", str(current)) from error
    return candidate


def _files(root: Path, requested: Path) -> list[Path]:
    candidate = _contained(root, requested)
    mode = candidate.lstat().st_mode
    if stat.S_ISREG(mode):
        return [candidate]
    if not stat.S_ISDIR(mode):
        raise ScanInputError("input:not-file-or-directory", str(candidate))
    files: list[Path] = []
    pending = [candidate]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name, reverse=True)
        except OSError as error:
            raise ScanInputError("input:unreadable", str(directory)) from error
        for entry in entries:
            entry_path = Path(entry.path)
            if entry.is_symlink():
                raise ScanInputError("input:symlink", str(entry_path))
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry_path)
            elif entry.is_file(follow_symlinks=False):
                files.append(entry_path)
            else:
                raise ScanInputError("input:not-file-or-directory", str(entry_path))
    return sorted(files)


def scan_paths(
    root: Path, paths: Iterable[Path], *, canaries: Iterable[str | bytes]
) -> list[Finding]:
    """Scan required files contained by ``root`` and return sorted findings."""
    resolved_root = _validated_root(root, rule_prefix="input")
    scanner = CredentialLeakScanner(canaries)
    findings: set[Finding] = set()
    for requested in paths:
        if not requested.is_absolute():
            requested = resolved_root / requested
        for resolved in _files(resolved_root, requested):
            surface = resolved.relative_to(resolved_root).as_posix()
            findings.update(scanner.scan_file(resolved, surface=surface))
    return _sorted(findings)


def _sorted(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda item: (item.surface, item.rule, item.fingerprint))


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("paths", type=Path, nargs="+")
    return parser


def _canaries_from_environment() -> list[str]:
    raw = os.environ.get("ZEROTH_SECURITY_CANARIES")
    if raw is None:
        return []
    try:
        canaries = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ScanInputError("input:canaries-invalid", raw) from error
    if not isinstance(canaries, list) or not canaries or not all(
        isinstance(item, str) and item for item in canaries
    ):
        raise ScanInputError("input:canaries-invalid", raw)
    return canaries


def _output_path(root: Path, requested: Path) -> Path:
    resolved_root = _validated_root(root, rule_prefix="output")
    if ".." in requested.parts:
        raise ScanInputError("output:parent-traversal", str(requested))
    candidate = requested if requested.is_absolute() else resolved_root / requested
    candidate = _absolute(candidate)
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError:
        raise ScanInputError("output:outside-root", str(requested)) from None
    current = resolved_root
    try:
        for part in relative.parts[:-1]:
            current = current / part
            status = current.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise ScanInputError("output:unsafe-path", str(current))
        if (candidate.exists() or candidate.is_symlink()) and stat.S_ISLNK(
            candidate.lstat().st_mode
        ):
            raise ScanInputError("output:unsafe-path", str(candidate))
    except ScanInputError:
        raise
    except OSError as error:
        raise ScanInputError("output:unwritable", str(current)) from error
    return candidate


def _emit(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise ScanInputError("output:unwritable", str(output)) from error


def main(argv: list[str] | None = None) -> int:
    output: Path | None = None
    try:
        args = _parser().parse_args(argv)
        output = _output_path(args.root, args.output) if args.output is not None else None
        findings = scan_paths(args.root, args.paths, canaries=_canaries_from_environment())
    except ScanInputError as error:
        report = {
            "diagnostics": [error.diagnostic.as_dict()],
            "findings": [],
            "status": "error",
        }
        try:
            _emit(report, output)
        except ScanInputError:
            print(json.dumps(report, sort_keys=True))
        return 2
    report = {
        "findings": [finding.as_dict() for finding in findings],
        "status": "failed" if findings else "passed",
    }
    try:
        _emit(report, output)
    except ScanInputError as error:
        fallback = {"diagnostics": [error.diagnostic.as_dict()], "findings": [], "status": "error"}
        print(json.dumps(fallback, sort_keys=True))
        return 2
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
