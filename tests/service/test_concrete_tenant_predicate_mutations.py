"""Mutation checks for production repositories with custom tenant SQL."""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from zeroth.governance.attestations import (
    InventoryRegistrationRepository,
    RunAttestationRepository,
)
from zeroth.governance.decisions import DecisionRepository
from zeroth.platform.storage import ResourceOperation
from zeroth.platform.storage.scoped_table import _StructuredTable
from zeroth.platform.storage.service_surfaces import (
    executable_probe_for,
    load_service_persistence_surfaces,
)

_OPERATION_CODE = {
    "c": ResourceOperation.CREATE,
    "r": ResourceOperation.READ,
    "n": ResourceOperation.ENUMERATE,
    "u": ResourceOperation.UPDATE,
    "d": ResourceOperation.DELETE,
}

_PREDICATE_MECHANISMS = (
    (
        "service.enforcement_heartbeats",
        "zeroth.governance.attestations.heartbeat",
        "_SELECT_LATEST_HEARTBEAT",
        # ``record`` does not execute this lookup; CREATE ownership binding is
        # covered by the generated ScopedTable gateway mutation instead.
        "r",
    ),
)

_MUTATIONS = tuple(
    (resource_name, module_name, predicate_name, _OPERATION_CODE[code])
    for resource_name, module_name, predicate_name, operation_codes in _PREDICATE_MECHANISMS
    for code in operation_codes
)
_SURFACES = load_service_persistence_surfaces()


@pytest.mark.parametrize(
    ("repository_type", "gateway_name"),
    (
        (InventoryRegistrationRepository, "_registrations"),
        (RunAttestationRepository, "_attestations"),
        (DecisionRepository, "_decisions"),
    ),
)
@pytest.mark.parametrize("tenant_id", ("default", "tenant-alpha"))
def test_scoped_gateway_factories_preserve_default_and_named_tenants(
    async_database,
    repository_type,
    gateway_name: str,
    tenant_id: str,
) -> None:
    repository = repository_type(async_database)
    gateway = getattr(repository, gateway_name)(tenant_id)

    assert gateway._context.tenant_id == tenant_id


def test_custom_predicate_mechanism_inventory_matches_production() -> None:
    reviewed = {
        (module_name, predicate_name) for _, module_name, predicate_name, _ in _PREDICATE_MECHANISMS
    }
    direct_database_modules = {
        surface.repository_type.__module__
        for surface in _SURFACES
        if "self._database.transaction(" in inspect.getsource(surface.repository_type)
    }
    assert {module_name for module_name, _ in reviewed} == direct_database_modules
    for module_name, predicate_name in reviewed:
        module = importlib.import_module(module_name)
        assert hasattr(module, predicate_name)
        loads = {
            node.id
            for node in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        assert predicate_name in loads


@pytest.mark.parametrize(
    ("resource_name", "module_name", "predicate_name", "operation"),
    _MUTATIONS,
    ids=[f"{item[0]}:{item[3].value}" for item in _MUTATIONS],
)
async def test_each_concrete_repository_predicate_bypass_is_detected(
    async_database,
    monkeypatch,
    resource_name: str,
    module_name: str,
    predicate_name: str,
    operation: ResourceOperation,
) -> None:
    module = importlib.import_module(module_name)
    predicate = getattr(module, predicate_name)
    if isinstance(predicate, str):
        bypass = predicate.replace("tenant_id = ?", "? IS NOT NULL")
        assert bypass != predicate
    else:

        def bypass(*_):
            return None, ()

    monkeypatch.setattr(module, predicate_name, bypass)
    probe = executable_probe_for(_SURFACES, resource_name, operation)

    with pytest.raises(AssertionError):
        await probe(async_database, operation=operation)


async def test_audit_chain_read_predicate_bypass_is_detected(async_database, monkeypatch) -> None:
    original_where = _StructuredTable._where

    def without_chain_head_scope(
        self,
        where,
        *,
        qualifier=None,
        include_scope=True,
        definition,
    ):
        return original_where(
            self,
            where,
            qualifier=qualifier,
            include_scope=(
                include_scope and definition.resource_name != "service.audit_chain_heads"
            ),
            definition=definition,
        )

    monkeypatch.setattr(_StructuredTable, "_where", without_chain_head_scope)
    probe = executable_probe_for(_SURFACES, "service.audit_chain_heads", ResourceOperation.READ)

    with pytest.raises(AssertionError):
        await probe(async_database, operation=ResourceOperation.READ)
