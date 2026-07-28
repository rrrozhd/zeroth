"""Executable-unit integrity and admission control helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from zeroth.integrations.execution.sandbox import _canonical_json


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
    """Outcome of admitting a manifest for execution.

    ``reason`` is a stable ``snake_case`` code naming the branch that decided
    the verdict, never a rendered message: it is promoted into the audit trail
    as decision metadata, which is retained where free-form text is not.
    """

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
        """Return the verdict for one manifest, naming the branch that decided it.

        ``reason`` is a stable ``snake_case`` code on every branch, matching the
        ``trusted_digest`` the admitted branch already returned. The rejected
        manifest's own text (its runtime, its command, its unit id) stays out of
        it: the reason travels into an audit record, where a producer-authored
        string is content rather than decision metadata, and the caller's
        exception message still names the binding that was refused.
        """
        digest = compute_manifest_digest(manifest)
        runtime = manifest.runtime.value
        if self.allowed_runtimes and runtime not in self.allowed_runtimes:
            return AdmissionResult(False, "runtime_not_allowed", digest)
        command = self._command_identity(manifest)
        if self.allowed_commands and command not in self.allowed_commands:
            return AdmissionResult(False, "command_not_allowed", digest)
        trusted_digest = self._trusted_digests.get(manifest.unit_id)
        if trusted_digest is None:
            return AdmissionResult(False, "no_trusted_digest_registered", digest)
        if trusted_digest != digest:
            return AdmissionResult(False, "trusted_digest_mismatch", digest)
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
