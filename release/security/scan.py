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
import secrets
import stat
import sys
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

_CHUNK_SIZE = 64 * 1024
# Normalization is one bounded pass per window. Canaries are bounded so their
# worst-case JSON representation plus overlap always fits inside that window.
_MAX_NORMALIZATION_WINDOW = 1 << 20
_MAX_CANARY_BYTES = 16 * 1024
_MIN_CANARY_BYTES = 8
_MIN_CANARY_DISTINCT_BYTES = 4
_FINGERPRINT_PREFIX = "sha256:"
_GITHUB_PATTERNS = (
    (
        "github:classic",
        re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36}(?![A-Za-z0-9_])"),
    ),
    (
        "github:fine-grained",
        re.compile(rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{82,255}(?![A-Za-z0-9_])"),
    ),
)
_PERCENT_ESCAPE = re.compile(rb"%([0-9A-Fa-f]{2})")
_JSON_UNICODE_ESCAPE = re.compile(rb"\\[uU]([0-9A-Fa-f]{4})")
_JSON_SURROGATE_PAIR = re.compile(
    rb"\\[uU]([dD][89ABab][0-9A-Fa-f]{2})\\[uU]([dD][C-Fc-f][0-9A-Fa-f]{2})"
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


@dataclass
class _OutputTarget:
    parent_fd: int
    parent_path: Path
    name: str

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.parent_fd)


class ScanInputError(ValueError):
    """A required scanner input could not safely be consumed."""

    def __init__(self, rule: str, value: str) -> None:
        self.diagnostic = Finding(
            fingerprint=_fingerprint(value.encode("utf-8", errors="replace")),
            rule=rule,
            surface="required-input",
        )
        super().__init__("required scanner input rejected")


class CanaryError(ScanInputError):
    """A canary is too weak to produce trustworthy derived matches."""

    def __init__(self) -> None:
        super().__init__("input:canary-too-weak", "rejected-canary")


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


def _decode_percent(value: bytes) -> bytes:
    return _PERCENT_ESCAPE.sub(lambda match: bytes([int(match.group(1), 16)]), value)


def _decode_json(value: bytes) -> bytes:
    def _surrogate_pair(match: re.Match[bytes]) -> bytes:
        high = int(match.group(1), 16)
        low = int(match.group(2), 16)
        codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
        return chr(codepoint).encode("utf-8")

    def _unicode(match: re.Match[bytes]) -> bytes:
        try:
            return chr(int(match.group(1), 16)).encode("utf-8")
        except UnicodeEncodeError:
            return match.group(0)

    decoded = _JSON_SURROGATE_PAIR.sub(_surrogate_pair, value)
    decoded = _JSON_UNICODE_ESCAPE.sub(_unicode, decoded)
    return decoded.replace(b"\\/", b"/")


class CredentialLeakScanner:
    """Scan byte and structured observable surfaces for credentials."""

    def __init__(self, canaries: Iterable[str | bytes]) -> None:
        needles: list[tuple[str, bytes, str]] = []
        patterns: list[tuple[str, re.Pattern[bytes], str, bytes | None]] = []
        canonical_canaries: list[tuple[bytes, str]] = []
        seen_variants: set[tuple[bytes, str]] = set()
        for raw_canary in canaries:
            canary = raw_canary if isinstance(raw_canary, bytes) else raw_canary.encode()
            if (
                len(canary) < _MIN_CANARY_BYTES
                or len(canary) > _MAX_CANARY_BYTES
                or len(set(canary)) < _MIN_CANARY_DISTINCT_BYTES
            ):
                raise CanaryError()
            fingerprint = _fingerprint(canary)
            canonical_canaries.append((canary, fingerprint))
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
                variants["canary:json"] = {json.dumps(text, ensure_ascii=True)[1:-1].encode()}
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
        self._canaries = tuple(canonical_canaries)
        self._overlap = max(
            [
                300,
                *(len(needle) - 1 for _, needle, _ in self._needles),
                *(6 * len(canary) - 1 for canary, _ in self._canaries),
            ],
            default=300,
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
            if (match := pattern.search(value)) and (required is None or required in match.group(0))
        )
        findings.update(self._semantic_findings(value, surface=safe_surface))
        for rule, pattern in _GITHUB_PATTERNS:
            findings.update(
                Finding(fingerprint=_fingerprint(match.group(0)), rule=rule, surface=safe_surface)
                for match in pattern.finditer(value)
            )
        return _sorted(findings)

    def _semantic_findings(self, value: bytes, *, surface: str) -> set[Finding]:
        findings: set[Finding] = set()
        step = max(1, _MAX_NORMALIZATION_WINDOW - self._overlap)
        for start in range(0, len(value) or 1, step):
            window = value[start : start + _MAX_NORMALIZATION_WINDOW]
            percent = _decode_percent(window)
            escaped_json = _decode_json(window)
            for canary, fingerprint in self._canaries:
                if percent != window and canary in percent:
                    findings.add(Finding(fingerprint, "canary:url", surface))
                if escaped_json != window and canary in escaped_json:
                    findings.add(Finding(fingerprint, "canary:json", surface))
            if start + _MAX_NORMALIZATION_WINDOW >= len(value):
                break
        return findings

    def _safe_surface(self, surface: str) -> str:
        encoded = surface.encode("utf-8", errors="replace")
        contains_canary = any(needle in encoded for _, needle, _ in self._needles)
        contains_encoded_canary = any(
            (match := pattern.search(encoded)) and (required is None or required in match.group(0))
            for _, pattern, _, required in self._patterns
        )
        contains_semantic_canary = bool(self._semantic_findings(encoded, surface="label"))
        contains_github_token = any(pattern.search(encoded) for _, pattern in _GITHUB_PATTERNS)
        if (
            contains_canary
            or contains_encoded_canary
            or contains_semantic_canary
            or contains_github_token
        ):
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

    def scan_descriptor(self, descriptor: int, *, surface: str) -> list[Finding]:
        """Scan an already-open regular file without resolving a pathname."""
        findings: set[Finding] = set()
        tail = b""
        duplicate: int | None = None
        try:
            duplicate = os.dup(descriptor)
            with os.fdopen(duplicate, "rb") as handle:
                duplicate = None
                while chunk := handle.read(_CHUNK_SIZE):
                    window = tail + chunk
                    findings.update(self.scan_bytes(window, surface=surface))
                    tail = window[-self._overlap :]
        except OSError as error:
            raise ScanInputError("input:unreadable", surface) from error
        finally:
            if duplicate is not None:
                with suppress(OSError):
                    os.close(duplicate)
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


def _open_child_descriptor(parent_fd: int, name: str, *, directory: bool) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ScanInputError("platform:descriptor-isolation-unavailable", name)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    return os.open(name, flags, dir_fd=parent_fd)


def _open_root_descriptor(root: Path, *, rule_prefix: str) -> tuple[int, Path]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ScanInputError("platform:descriptor-isolation-unavailable", str(root))
    lexical_root = _validated_root(root, rule_prefix=rule_prefix)
    anchor = Path(lexical_root.anchor)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            anchor,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        for part in lexical_root.parts[1:]:
            child = _open_child_descriptor(descriptor, part, directory=True)
            with suppress(OSError):
                os.close(descriptor)
            descriptor = child
        return descriptor, lexical_root
    except OSError as error:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise ScanInputError(f"{rule_prefix}:root-unreadable", str(root)) from error


def _relative_parts(root: Path, requested: Path, *, rule_prefix: str) -> tuple[str, ...]:
    if ".." in requested.parts:
        raise ScanInputError(f"{rule_prefix}:parent-traversal", str(requested))
    candidate = requested if requested.is_absolute() else root / requested
    candidate = _absolute(candidate)
    try:
        return candidate.relative_to(root).parts
    except ValueError:
        raise ScanInputError(f"{rule_prefix}:outside-root", str(requested)) from None


def _scan_open_entry(
    scanner: CredentialLeakScanner,
    parent_fd: int,
    name: str,
    *,
    surface: str,
    expected_identity: tuple[int, int, str] | None = None,
) -> set[Finding]:
    descriptor: int | None = None
    try:
        descriptor = _open_child_descriptor(parent_fd, name, directory=False)
        descriptor_status = os.fstat(descriptor)
        mode = descriptor_status.st_mode
        actual_kind = (
            "file" if stat.S_ISREG(mode) else "directory" if stat.S_ISDIR(mode) else "other"
        )
        actual_identity = (descriptor_status.st_dev, descriptor_status.st_ino, actual_kind)
        if expected_identity is not None and actual_identity != expected_identity:
            raise ScanInputError("input:type-changed", surface)
        if stat.S_ISREG(mode):
            return set(scanner.scan_descriptor(descriptor, surface=surface))
        if not stat.S_ISDIR(mode):
            raise ScanInputError("input:not-file-or-directory", surface)
        findings: set[Finding] = set()
        for child_name, expected in _directory_entries(descriptor, surface=surface):
            child_surface = f"{surface}/{child_name}" if surface else child_name
            findings.update(
                _scan_open_entry(
                    scanner,
                    descriptor,
                    child_name,
                    surface=child_surface,
                    expected_identity=expected,
                )
            )
        return findings
    except ScanInputError:
        raise
    except OSError as error:
        raise ScanInputError("input:unreadable", surface) from error
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _directory_entries(descriptor: int, *, surface: str) -> list[tuple[str, tuple[int, int, str]]]:
    try:
        result: list[tuple[str, tuple[int, int, str]]] = []
        with os.scandir(descriptor) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
            for entry in entries:
                child_status = entry.stat(follow_symlinks=False)
                child_mode = child_status.st_mode
                if stat.S_ISLNK(child_mode):
                    raise ScanInputError("input:symlink", surface)
                kind = (
                    "file"
                    if stat.S_ISREG(child_mode)
                    else "directory"
                    if stat.S_ISDIR(child_mode)
                    else "other"
                )
                result.append((entry.name, (child_status.st_dev, child_status.st_ino, kind)))
        return result
    except ScanInputError:
        raise
    except OSError as error:
        raise ScanInputError("input:unreadable", surface) from error


def _scan_from_root(
    root_fd: int,
    root: Path,
    paths: Iterable[Path],
    *,
    canaries: Iterable[str | bytes],
) -> list[Finding]:
    scanner = CredentialLeakScanner(canaries)
    findings: set[Finding] = set()
    for requested in paths:
        parts = _relative_parts(root, requested, rule_prefix="input")
        descriptor: int | None = None
        try:
            descriptor = os.dup(root_fd)
            if not parts:
                for name, expected in _directory_entries(descriptor, surface="root"):
                    findings.update(
                        _scan_open_entry(
                            scanner,
                            descriptor,
                            name,
                            surface=name,
                            expected_identity=expected,
                        )
                    )
                continue
            for part in parts[:-1]:
                child = _open_child_descriptor(descriptor, part, directory=True)
                os.close(descriptor)
                descriptor = child
            findings.update(
                _scan_open_entry(
                    scanner,
                    descriptor,
                    parts[-1],
                    surface="/".join(parts),
                )
            )
        except ScanInputError:
            raise
        except OSError as error:
            raise ScanInputError("input:unreadable", str(requested)) from error
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
    return _sorted(findings)


def scan_paths(
    root: Path, paths: Iterable[Path], *, canaries: Iterable[str | bytes]
) -> list[Finding]:
    """Scan required files contained by ``root`` and return sorted findings."""
    root_fd, resolved_root = _open_root_descriptor(root, rule_prefix="input")
    try:
        return _scan_from_root(root_fd, resolved_root, paths, canaries=canaries)
    finally:
        with suppress(OSError):
            os.close(root_fd)


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
    if (
        not isinstance(canaries, list)
        or not canaries
        or not all(isinstance(item, str) and item for item in canaries)
    ):
        raise ScanInputError("input:canaries-invalid", raw)
    return canaries


def _output_target(root_fd: int, root: Path, requested: Path) -> _OutputTarget:
    parts = _relative_parts(root, requested, rule_prefix="output")
    if not parts:
        raise ScanInputError("output:invalid", str(requested))
    descriptor = os.dup(root_fd)
    current = root
    try:
        for part in parts[:-1]:
            current = current / part
            child = _open_child_descriptor(descriptor, part, directory=True)
            os.close(descriptor)
            descriptor = child
        try:
            target_status = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(target_status.st_mode) or not stat.S_ISREG(target_status.st_mode):
                raise ScanInputError("output:unsafe-path", str(requested))
        return _OutputTarget(descriptor, current, parts[-1])
    except ScanInputError:
        with suppress(OSError):
            os.close(descriptor)
        raise
    except OSError as error:
        with suppress(OSError):
            os.close(descriptor)
        raise ScanInputError("output:unwritable", str(current)) from error


def _verify_output_parent(target: _OutputTarget) -> None:
    try:
        path_status = target.parent_path.lstat()
        descriptor_status = os.fstat(target.parent_fd)
    except OSError as error:
        raise ScanInputError("output:parent-changed", str(target.parent_path)) from error
    if (
        stat.S_ISLNK(path_status.st_mode)
        or path_status.st_dev != descriptor_status.st_dev
        or path_status.st_ino != descriptor_status.st_ino
    ):
        raise ScanInputError("output:parent-changed", str(target.parent_path))


def _create_temp_descriptor(target: _OutputTarget) -> tuple[int, str]:
    for _ in range(16):
        name = f".security-scan-{secrets.token_hex(8)}"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target.parent_fd,
            )
            return descriptor, name
        except FileExistsError:
            continue
    raise ScanInputError("output:temporary-collision", target.name)


def _emit(report: dict[str, Any], output: _OutputTarget | None) -> None:
    rendered = json.dumps(report, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    temporary_name: str | None = None
    temporary_fd: int | None = None
    try:
        _verify_output_parent(output)
        temporary_fd, temporary_name = _create_temp_descriptor(output)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            temporary_fd = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        _verify_output_parent(output)
        os.replace(
            temporary_name,
            output.name,
            src_dir_fd=output.parent_fd,
            dst_dir_fd=output.parent_fd,
        )
        temporary_name = None
        os.fsync(output.parent_fd)
    except OSError as error:
        raise ScanInputError("output:unwritable", output.name) from error
    finally:
        if temporary_fd is not None:
            with suppress(OSError):
                os.close(temporary_fd)
        if temporary_name is not None:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=output.parent_fd)


def main(argv: list[str] | None = None) -> int:
    output: _OutputTarget | None = None
    root_fd: int | None = None
    try:
        args = _parser().parse_args(argv)
        root_fd, root = _open_root_descriptor(args.root, rule_prefix="input")
        output = _output_target(root_fd, root, args.output) if args.output is not None else None
        findings = _scan_from_root(root_fd, root, args.paths, canaries=_canaries_from_environment())
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
        if output is not None:
            output.close()
        if root_fd is not None:
            with suppress(OSError):
                os.close(root_fd)
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
        if output is not None:
            output.close()
        if root_fd is not None:
            with suppress(OSError):
                os.close(root_fd)
        return 2
    if output is not None:
        output.close()
    if root_fd is not None:
        with suppress(OSError):
            os.close(root_fd)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
