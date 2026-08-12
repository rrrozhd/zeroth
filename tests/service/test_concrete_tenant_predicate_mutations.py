"""Mutation checks for production repositories with custom tenant SQL."""

from __future__ import annotations

import importlib

import pytest

from zeroth.platform.storage import ResourceOperation
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
        "service.approvals",
        "zeroth.governance.approvals.repository",
        "_tenant_predicate",
        "crnu",
    ),
    (
        "service.approvals",
        "zeroth.governance.approvals.repository",
        "_ownership_conflict_clause",
        "c",
    ),
    (
        "service.decision_records",
        "zeroth.governance.decisions.repository",
        "_SELECT_BY_KEY",
        "cr",
    ),
    (
        "service.deployment_versions",
        "zeroth.service.deployments.repository",
        "_scope_clause",
        "crnu",
    ),
    (
        "service.graph_versions",
        "zeroth.contracts.graph.repository",
        "_scope_clause",
        "crnu",
    ),
    (
        "service.enforcement_heartbeats",
        "zeroth.governance.attestations.heartbeat",
        "_SELECT_LATEST_HEARTBEAT",
        "cr",
    ),
    (
        "service.memory_connector_configs",
        "zeroth.integrations.memory.config_repository",
        "_tenant_predicate",
        "crnud",
    ),
    (
        "service.run_attestations",
        "zeroth.governance.attestations.store",
        "_SELECT_ATTESTATION",
        "cr",
    ),
    (
        "service.tool_inventory_registrations",
        "zeroth.governance.attestations.store",
        "_SELECT_LATEST_REGISTRATION",
        "cr",
    ),
)

_MUTATIONS = tuple(
    (resource_name, module_name, predicate_name, _OPERATION_CODE[code])
    for resource_name, module_name, predicate_name, operation_codes in _PREDICATE_MECHANISMS
    for code in operation_codes
)
_SURFACES = load_service_persistence_surfaces()


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
    elif predicate_name == "_scope_clause":

        def bypass(*_):
            return "1=1", ()

    elif predicate_name == "_ownership_conflict_clause":

        def bypass():
            return ""

    else:

        def bypass(*_):
            return None, ()

    monkeypatch.setattr(module, predicate_name, bypass)
    probe = executable_probe_for(_SURFACES, resource_name, operation)

    with pytest.raises(AssertionError):
        await probe(async_database, operation=operation)
