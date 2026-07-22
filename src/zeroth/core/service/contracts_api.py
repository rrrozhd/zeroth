"""Legacy import location for the contracts api module.

The definitions now live in :mod:`zeroth.service.api.contracts_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.contracts_api import ContractApiBootstrapLike as ContractApiBootstrapLike
from zeroth.service.api.contracts_api import (
    DeploymentResultErrorStateSchemaResponse as DeploymentResultErrorStateSchemaResponse,
)
from zeroth.service.api.contracts_api import (
    DeploymentVersionMetadataResponse as DeploymentVersionMetadataResponse,
)
from zeroth.service.api.contracts_api import (
    PublicContractSchemaResponse as PublicContractSchemaResponse,
)
from zeroth.service.api.contracts_api import _bootstrap as _bootstrap
from zeroth.service.api.contracts_api import _deployment_context as _deployment_context
from zeroth.service.api.contracts_api import _resolve_contract_version as _resolve_contract_version
from zeroth.service.api.contracts_api import _serialize_contract as _serialize_contract
from zeroth.service.api.contracts_api import register_contract_routes as register_contract_routes
from zeroth.service.api.contracts_api import (
    serialize_deployment_metadata as serialize_deployment_metadata,
)
