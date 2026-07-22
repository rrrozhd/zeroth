"""Legacy import location for the approval api module.

The definitions now live in :mod:`zeroth.service.api.approval_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.approval_api import ApprovalApiBootstrapLike as ApprovalApiBootstrapLike
from zeroth.service.api.approval_api import ApprovalResolutionRequest as ApprovalResolutionRequest
from zeroth.service.api.approval_api import ApprovalResolutionResponse as ApprovalResolutionResponse
from zeroth.service.api.approval_api import _approval_matches_filters as _approval_matches_filters
from zeroth.service.api.approval_api import (
    _approval_visible_to_deployment as _approval_visible_to_deployment,
)
from zeroth.service.api.approval_api import _bootstrap as _bootstrap
from zeroth.service.api.approval_api import _deployment_context as _deployment_context
from zeroth.service.api.approval_api import (
    _require_pending_visible_approval as _require_pending_visible_approval,
)
from zeroth.service.api.approval_api import _require_visible_approval as _require_visible_approval
from zeroth.service.api.approval_api import (
    _run_is_waiting_for_approval as _run_is_waiting_for_approval,
)
from zeroth.service.api.approval_api import register_approval_routes as register_approval_routes
