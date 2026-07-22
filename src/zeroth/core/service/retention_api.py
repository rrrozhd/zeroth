"""Legacy import location for the retention api module.

The definitions now live in :mod:`zeroth.service.api.retention_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.retention_api import ErasureRequestBody as ErasureRequestBody
from zeroth.service.api.retention_api import ErasureResponse as ErasureResponse
from zeroth.service.api.retention_api import ErasureRunResult as ErasureRunResult
from zeroth.service.api.retention_api import LegalHoldBody as LegalHoldBody
from zeroth.service.api.retention_api import LegalHoldResponse as LegalHoldResponse
from zeroth.service.api.retention_api import RetentionBootstrapLike as RetentionBootstrapLike
from zeroth.service.api.retention_api import RetentionPolicyBody as RetentionPolicyBody
from zeroth.service.api.retention_api import RetentionPolicyResponse as RetentionPolicyResponse
from zeroth.service.api.retention_api import _bootstrap as _bootstrap
from zeroth.service.api.retention_api import _hold_response as _hold_response
from zeroth.service.api.retention_api import _policy_response as _policy_response
from zeroth.service.api.retention_api import _run_result as _run_result
from zeroth.service.api.retention_api import register_retention_routes as register_retention_routes
