"""Legacy import location for the audit api module.

The definitions now live in :mod:`zeroth.service.api.audit_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.audit_api import _REDACTOR as _REDACTOR
from zeroth.service.api.audit_api import (
    AttestationVerificationResponse as AttestationVerificationResponse,
)
from zeroth.service.api.audit_api import AuditApiBootstrapLike as AuditApiBootstrapLike
from zeroth.service.api.audit_api import AuditRecordListResponse as AuditRecordListResponse
from zeroth.service.api.audit_api import AuditTimelineResponse as AuditTimelineResponse
from zeroth.service.api.audit_api import AuditVerificationResponse as AuditVerificationResponse
from zeroth.service.api.audit_api import (
    DeploymentAttestationResponse as DeploymentAttestationResponse,
)
from zeroth.service.api.audit_api import DeploymentEvidenceResponse as DeploymentEvidenceResponse
from zeroth.service.api.audit_api import EvidenceSummaryResponse as EvidenceSummaryResponse
from zeroth.service.api.audit_api import RunEvidenceResponse as RunEvidenceResponse
from zeroth.service.api.audit_api import VerifyChainRequest as VerifyChainRequest
from zeroth.service.api.audit_api import (
    _attestation_verification_response as _attestation_verification_response,
)
from zeroth.service.api.audit_api import _bootstrap as _bootstrap
from zeroth.service.api.audit_api import _deployment_context as _deployment_context
from zeroth.service.api.audit_api import _load_bound_deployment as _load_bound_deployment
from zeroth.service.api.audit_api import _sanitize_mapping as _sanitize_mapping
from zeroth.service.api.audit_api import _signer_key_id as _signer_key_id
from zeroth.service.api.audit_api import _verification_response as _verification_response
from zeroth.service.api.audit_api import _visible_approvals as _visible_approvals
from zeroth.service.api.audit_api import _visible_record as _visible_record
from zeroth.service.api.audit_api import register_audit_routes as register_audit_routes
