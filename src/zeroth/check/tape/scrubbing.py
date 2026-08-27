"""Deterministic high-confidence secret detection and replacement."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

_SECRET_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|secret|password|authorization|access[_-]?token|refresh[_-]?token|token)$",
    re.IGNORECASE,
)
_TOKEN = re.compile(
    r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._~+/-]{20,})"
)
_REPLACEMENT = re.compile(r"^<redacted:sha256:[0-9a-f]{16}>$")


@dataclass(frozen=True, slots=True)
class SecretFinding:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScrubResult:
    value: Any
    findings: tuple[SecretFinding, ...]


def _entropy(value: str) -> float:
    counts = Counter(value)
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_secret(value: str) -> str | None:
    if _REPLACEMENT.fullmatch(value):
        return None
    if _TOKEN.search(value):
        return "credential_pattern"
    if len(value) >= 28 and " " not in value and _entropy(value) >= 4.0:
        return "high_entropy"
    return None


class SecretScanner:
    """Scan JSON-shaped content without retaining secret values in findings."""

    def __init__(self, *, allowlist: set[str] | None = None) -> None:
        self._allowlist = frozenset(allowlist or ())

    def scan(self, value: Any) -> tuple[SecretFinding, ...]:
        findings: dict[str, SecretFinding] = {}

        def visit(item: Any, path: str, *, secret_key: bool = False) -> None:
            if type(item) is dict:
                for key, nested in item.items():
                    child = f"{path}.{key}"
                    visit(nested, child, secret_key=bool(_SECRET_KEY.search(key)))
                return
            if type(item) is list:
                for index, nested in enumerate(item):
                    visit(nested, f"{path}[{index}]", secret_key=secret_key)
                return
            if type(item) is not str or item in self._allowlist or _REPLACEMENT.fullmatch(item):
                return
            reason = "secret_key" if secret_key else _looks_secret(item)
            if reason is not None:
                findings[path] = SecretFinding(path=path, reason=reason)

        visit(value, "$")
        return tuple(findings.values())


def _replacement(value: str) -> str:
    return f"<redacted:sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}>"


def scrub_secrets(value: Any, *, allowlist: set[str] | None = None) -> ScrubResult:
    scanner = SecretScanner(allowlist=allowlist)
    findings = scanner.scan(value)
    paths = {finding.path for finding in findings}

    def replace(item: Any, path: str) -> Any:
        if type(item) is dict:
            return {key: replace(nested, f"{path}.{key}") for key, nested in item.items()}
        if type(item) is list:
            return [replace(nested, f"{path}[{index}]") for index, nested in enumerate(item)]
        if path in paths and type(item) is str:
            return _replacement(item)
        return item

    return ScrubResult(value=replace(value, "$"), findings=findings)
