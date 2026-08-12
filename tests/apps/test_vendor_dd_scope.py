from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from apps.vendor_dd.entrypoint import contract_registry_for_deployment
from zeroth.contracts.registry import ContractNotFoundError, ContractRegistry
from zeroth.platform.storage import ScopeContext


class VendorContract(BaseModel):
    value: str


async def test_entrypoint_contract_registry_uses_persisted_deployment_owner(sqlite_db) -> None:
    deployment = SimpleNamespace(tenant_id="tenant-vendor", workspace_id="workspace-dd")

    registry = contract_registry_for_deployment(sqlite_db, deployment)
    await registry.register(VendorContract, name="contract://vendor")

    same_scope = ContractRegistry.scoped(
        sqlite_db,
        ScopeContext(tenant_id="tenant-vendor", workspace_id="workspace-dd"),
    )
    other_scope = ContractRegistry.scoped(
        sqlite_db,
        ScopeContext(tenant_id="tenant-other", workspace_id="workspace-dd"),
    )
    assert (await same_scope.get("contract://vendor")).model_path.endswith(":VendorContract")
    with pytest.raises(ContractNotFoundError):
        await other_scope.get("contract://vendor")
