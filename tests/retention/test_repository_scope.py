from __future__ import annotations

import inspect

import pytest

from zeroth.governance.retention import (
    LegalHoldRepository,
    RetentionAuditLogRepository,
    RetentionPolicyRepository,
)
from zeroth.platform.storage import NullWorkspaceScopeContext


@pytest.mark.parametrize(
    "repository_type",
    [LegalHoldRepository, RetentionAuditLogRepository],
)
def test_retention_repository_constructors_require_scope_context(repository_type: type) -> None:
    parameters = inspect.signature(repository_type).parameters

    assert "scope_context" in parameters
    assert parameters["scope_context"].default is inspect.Parameter.empty


def test_policy_repository_requires_explicit_scope_context() -> None:
    parameters = inspect.signature(RetentionPolicyRepository).parameters

    assert parameters["scope_context"].default is inspect.Parameter.empty

    compatibility = inspect.signature(RetentionPolicyRepository.for_default_compatibility)
    assert compatibility.parameters["database"].default is inspect.Parameter.empty


async def test_foreign_legal_hold_read_and_list_match_unknown_scope(async_database) -> None:
    owner = LegalHoldRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-a"))
    foreign = LegalHoldRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-b"))
    placed = await owner.place(run_id="run-a", reason="review")

    assert await foreign.get(placed.hold_id) is None
    assert await foreign.get("unknown-hold") is None
    assert await foreign.list_for_tenant() == []
