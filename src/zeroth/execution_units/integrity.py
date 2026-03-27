"""Executable-unit integrity and admission control helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from zeroth.execution_units.sandbox import _canonical_json


def compute_manifest_digest(manifest: Any) -> str:
    """Compute a stable digest for a manifest, excluding embedded integrity metadata."""
    import hashlib

    payload = manifest.model_dump(mode="json", exclude={"integrity"})
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestIntegrityRecord:
    """Integrity metadata optionally attached to a manifest."""

    digest: str
    signed_at: datetime | None = None
    signer: str | None = None


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """Outcome of admitting a manifest for execution."""

    admitted: bool
    reason: str
    digest: str


@dataclass(slots=True)
class AdmissionController:
    """Check whether a manifest is trusted and allowed to run."""

    allowed_runtimes: set[str] = field(default_factory=set)
    allowed_commands: set[str] = field(default_factory=set)
    _trusted_digests: dict[str, str] = field(default_factory=dict)

    def __init__(
        self,
        *,
        allowed_runtimes: Iterable[str] | None = None,
        allowed_commands: Iterable[str] | None = None,
    ) -> None:
        self.allowed_runtimes = {item for item in allowed_runtimes or []}
        self.allowed_commands = {item for item in allowed_commands or []}
        self._trusted_digests = {}

    def register_trusted_digest(self, manifest_ref: str, digest: str) -> None:
        self._trusted_digests[manifest_ref] = digest

    def admit(self, manifest: Any) -> AdmissionResult:
        digest = compute_manifest_digest(manifest)
        runtime = manifest.runtime.value
        if self.allowed_runtimes and runtime not in self.allowed_runtimes:
            return AdmissionResult(False, f"runtime {runtime!r} is not allowed", digest)
        command = self._command_identity(manifest)
        if self.allowed_commands and command not in self.allowed_commands:
            return AdmissionResult(False, f"command {command!r} is not allowed", digest)
        trusted_digest = self._trusted_digests.get(manifest.unit_id)
        if trusted_digest is None:
            return AdmissionResult(
                False,
                f"no trusted digest registered for {manifest.unit_id}",
                digest,
            )
        if trusted_digest != digest:
            return AdmissionResult(False, "manifest digest does not match trusted digest", digest)
        return AdmissionResult(True, "trusted_digest", digest)

    def _command_identity(self, manifest: Any) -> str:
        if manifest.run_config.command:
            return manifest.run_config.command[0]
        return manifest.artifact_source.ref


__all__ = [
    "AdmissionController",
    "AdmissionResult",
    "ManifestIntegrityRecord",
    "compute_manifest_digest",
]
