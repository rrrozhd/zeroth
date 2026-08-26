"""Reusable generated-app certification contract and runner."""

from .evidence import (
    bind_sbom,
    finalize_attestation,
    validate_image_archive,
    validate_source_archive,
    verify_finalized_attestation,
    write_provenance,
)
from .models import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CandidateIdentity,
    CertificationReport,
    CertificationTargets,
    CheckResult,
    EvidenceBinding,
    EvidenceFile,
    SmokeSpec,
    evidence_binding_digest,
    file_digest,
    identity_digest,
    load_declaration,
    validate_report,
    write_report,
)
from .promotion import issue_promotion_receipt
from .runner import (
    CertificationRunner,
    CommandResult,
    HttpResult,
    execute_command,
    measure_candidate_identity,
)
from .scaffold import scaffold_checkout

__all__ = [
    "MANDATORY_CHECKS",
    "AppDeclaration",
    "CandidateIdentity",
    "CertificationTargets",
    "CertificationReport",
    "CertificationRunner",
    "CheckResult",
    "CommandResult",
    "EvidenceBinding",
    "EvidenceFile",
    "HttpResult",
    "SmokeSpec",
    "execute_command",
    "evidence_binding_digest",
    "file_digest",
    "finalize_attestation",
    "identity_digest",
    "issue_promotion_receipt",
    "load_declaration",
    "measure_candidate_identity",
    "scaffold_checkout",
    "bind_sbom",
    "validate_image_archive",
    "validate_source_archive",
    "verify_finalized_attestation",
    "validate_report",
    "write_report",
    "write_provenance",
]
