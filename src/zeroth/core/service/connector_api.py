"""Legacy import location for the connector api module.

The definitions now live in :mod:`zeroth.service.api.connector_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.connector_api import _PROBE_KEY as _PROBE_KEY
from zeroth.service.api.connector_api import _PROBE_TARGET as _PROBE_TARGET
from zeroth.service.api.connector_api import _PROBE_TIMEOUT_SECONDS as _PROBE_TIMEOUT_SECONDS
from zeroth.service.api.connector_api import _REF_RE as _REF_RE
from zeroth.service.api.connector_api import _USERINFO_RE as _USERINFO_RE
from zeroth.service.api.connector_api import ConnectorCreateRequest as ConnectorCreateRequest
from zeroth.service.api.connector_api import ConnectorSummaryResponse as ConnectorSummaryResponse
from zeroth.service.api.connector_api import ConnectorTestResponse as ConnectorTestResponse
from zeroth.service.api.connector_api import ConnectorUpdateRequest as ConnectorUpdateRequest
from zeroth.service.api.connector_api import _mask_params as _mask_params
from zeroth.service.api.connector_api import _mask_secret_string as _mask_secret_string
from zeroth.service.api.connector_api import _probe_connector as _probe_connector
from zeroth.service.api.connector_api import _registry_and_repo as _registry_and_repo
from zeroth.service.api.connector_api import _require_repo as _require_repo
from zeroth.service.api.connector_api import _summary as _summary
from zeroth.service.api.connector_api import _tenant as _tenant
from zeroth.service.api.connector_api import _validate_ref as _validate_ref
from zeroth.service.api.connector_api import register_connector_routes as register_connector_routes
