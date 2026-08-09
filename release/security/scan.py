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
import sys
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


class CredentialLeakScanner:
    """Scan byte and structured observable surfaces for credentials."""

    def __init__(self, canaries: Iterable[str | bytes]) -> None:
        needles: list[tuple[str, bytes, str]] = []
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
            for rule, encoded_values in variants.items():
                for value in encoded_values:
                    identity = (value, fingerprint)
                    if value and identity not in seen_variants:
                        needles.append((rule, value, fingerprint))
                        seen_variants.add(identity)
        self._needles = tuple(needles)
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
        contains_github_token = any(pattern.search(encoded) for _, pattern in _GITHUB_PATTERNS)
        if contains_canary or contains_github_token:
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
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_CHUNK_SIZE):
                    window = tail + chunk
                    findings.update(self.scan_bytes(window, surface=surface))
                    tail = window[-self._overlap :]
        except OSError as error:
            raise ScanInputError("input:unreadable", str(path)) from error
        return _sorted(findings)


def _inside(root: Path, path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ScanInputError("input:unreadable", str(path)) from error
    if not resolved.is_relative_to(root):
        raise ScanInputError("input:outside-root", str(path))
    if not resolved.is_file():
        raise ScanInputError("input:not-file", str(path))
    return resolved


def scan_paths(
    root: Path, paths: Iterable[Path], *, canaries: Iterable[str | bytes]
) -> list[Finding]:
    """Scan required files contained by ``root`` and return sorted findings."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ScanInputError("input:root-unreadable", str(root)) from error
    if not resolved_root.is_dir():
        raise ScanInputError("input:root-not-directory", str(root))
    scanner = CredentialLeakScanner(canaries)
    findings: set[Finding] = set()
    for requested in paths:
        if not requested.is_absolute():
            requested = resolved_root / requested
        resolved = _inside(resolved_root, requested)
        surface = resolved.relative_to(resolved_root).as_posix()
        findings.update(scanner.scan_file(resolved, surface=surface))
    return _sorted(findings)


def _sorted(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda item: (item.surface, item.rule, item.fingerprint))


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("paths", type=Path, nargs="+")
    return parser


def _canaries_from_environment() -> list[str]:
    raw = os.environ.get("ZEROTH_SECURITY_CANARIES")
    if raw is None:
        raise ScanInputError("input:canaries-missing", "ZEROTH_SECURITY_CANARIES")
    try:
        canaries = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ScanInputError("input:canaries-invalid", raw) from error
    if not isinstance(canaries, list) or not canaries or not all(
        isinstance(item, str) and item for item in canaries
    ):
        raise ScanInputError("input:canaries-invalid", raw)
    return canaries


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        findings = scan_paths(args.root, args.paths, canaries=_canaries_from_environment())
    except ScanInputError as error:
        report = {
            "diagnostics": [error.diagnostic.as_dict()],
            "findings": [],
            "status": "error",
        }
        print(json.dumps(report, sort_keys=True))
        return 2
    report = {
        "findings": [finding.as_dict() for finding in findings],
        "status": "failed" if findings else "passed",
    }
    print(json.dumps(report, sort_keys=True))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
