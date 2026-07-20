"""Legacy import path for :mod:`zeroth.integrations.execution.integrity`."""

from zeroth.integrations.execution.integrity import (
    AdmissionController,
    AdmissionResult,
    ManifestIntegrityRecord,
    compute_manifest_digest,
)

__all__ = [
    "AdmissionController",
    "AdmissionResult",
    "ManifestIntegrityRecord",
    "compute_manifest_digest",
]
