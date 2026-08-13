"""Reusable generated-app certification contract and runner."""

from .models import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CandidateIdentity,
    CertificationReport,
    CheckResult,
    EvidenceBinding,
    EvidenceFile,
    SmokeSpec,
    load_declaration,
    validate_report,
    write_report,
)
from .runner import (
    CertificationRunner,
    CommandResult,
    HttpResult,
    execute_command,
    identity_digest,
    measure_candidate_identity,
)

__all__ = [
    "MANDATORY_CHECKS",
    "AppDeclaration",
    "CandidateIdentity",
    "CertificationReport",
    "CertificationRunner",
    "CheckResult",
    "CommandResult",
    "EvidenceBinding",
    "EvidenceFile",
    "HttpResult",
    "SmokeSpec",
    "execute_command",
    "identity_digest",
    "load_declaration",
    "measure_candidate_identity",
    "validate_report",
    "write_report",
]
