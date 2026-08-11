from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy import (
    String,
    bindparam,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.orm import Mapped, Session, aliased, mapped_column

from zeroth.econ.plane.auth.models import User
from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.costing.models import PricingCatalog
from zeroth.econ.plane.database import Base
from zeroth.econ.plane import database
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    ResourceScopeDefinition,
    ScopeContext,
    TenantWideScopeContext,
)

_ALL_OPERATIONS = frozenset(ResourceOperation)


class WorkspaceRecord(Base):
    __tablename__ = "test_workspace_records"

    scope_definition: ClassVar[ResourceScopeDefinition] = ResourceScopeDefinition(
        resource_name="test.workspace_record",
        table_name=__tablename__,
        operations=_ALL_OPERATIONS,
        workspace_scoped=True,
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(String(128))


@pytest.fixture
def scoped_engine():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(bind=engine)
    return engine


def _scope(tenant: str = "tenant-a", workspace: str = "workspace-a") -> ScopeContext:
    return ScopeContext(tenant_id=tenant, workspace_id=workspace)


def _seed_capabilities(engine) -> None:
    with Session(engine) as raw:
        raw.add_all(
            [
                Capability(id="cap-a", tenant_id="tenant-a", name="A"),
                Capability(id="cap-b", tenant_id="tenant-b", name="B"),
            ]
        )
        raw.commit()


def test_select_hides_rows_owned_by_another_tenant(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        rows = scoped.scalars(select(Capability)).all()
        hidden = scoped.get(Capability, "cap-b")

    assert [row.id for row in rows] == ["cap-a"]
    assert hidden is None


def test_bulk_update_only_changes_rows_owned_by_bound_tenant(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        scoped.execute(update(Capability).values(name="changed"))
        scoped.commit()

    with Session(scoped_engine) as raw:
        names = dict(raw.execute(select(Capability.id, Capability.name)).all())
    assert names == {"cap-a": "changed", "cap-b": "B"}


def test_bulk_delete_only_removes_rows_owned_by_bound_tenant(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        scoped.execute(delete(Capability))
        scoped.commit()

    with Session(scoped_engine) as raw:
        remaining = raw.scalars(select(Capability.id)).all()
    assert remaining == ["cap-b"]


def test_flush_fills_missing_tenant_ownership(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        capability = Capability(id="cap-a", name="A")
        scoped.add(capability)
        scoped.flush()
        assert capability.tenant_id == "tenant-a"


def test_fresh_schema_user_is_tenant_owned(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        user = User(subject="subject-a", email="a@example.test")
        scoped.add(user)
        scoped.commit()
        assert user.tenant_id == "tenant-a"


def test_mismatching_insert_fails_before_sql(scoped_engine) -> None:
    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        scoped.add(Capability(id="cap-b", tenant_id="tenant-b", name="B"))
        statements.clear()
        with pytest.raises(ValueError, match="tenant ownership"):
            scoped.commit()

    assert statements == []


def test_bulk_ownership_rewrite_fails_before_sql(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)
    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        statements.clear()
        with pytest.raises(ValueError, match="tenant ownership"):
            scoped.execute(update(Capability).values(tenant_id="tenant-b"))

    assert statements == []


def test_executemany_update_rejects_ownership_in_any_parameter_before_sql(
    scoped_engine,
) -> None:
    _seed_capabilities(scoped_engine)
    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        statements.clear()
        with pytest.raises(ValueError, match="ownership is immutable"):
            scoped.execute(
                update(Capability).where(Capability.id == bindparam("target_id")),
                [
                    {"target_id": "cap-a", "name": "new-a"},
                    {
                        "target_id": "cap-b",
                        "tenant_id": "tenant-a",
                        "name": "new-b",
                    },
                ],
            )

    assert statements == []


def test_executemany_update_applies_tenant_predicate_to_entire_batch(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        scoped.execute(
            update(Capability)
            .where(Capability.id == bindparam("target_id"))
            .values(name=bindparam("new_name")),
            [
                {"target_id": "cap-a", "new_name": "new-a"},
                {"target_id": "cap-b", "new_name": "new-b"},
            ],
        )
        scoped.commit()

    with Session(scoped_engine) as raw:
        names = dict(raw.execute(select(Capability.id, Capability.name)).all())
    assert names == {"cap-a": "new-a", "cap-b": "B"}


def test_mapped_bulk_insert_fills_missing_tenant_ownership(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        scoped.execute(insert(Capability).values(id="cap-a", name="A"))
        scoped.commit()

    with Session(scoped_engine) as raw:
        tenant_id = raw.scalar(select(Capability.tenant_id).where(Capability.id == "cap-a"))
    assert tenant_id == "tenant-a"


def test_mapped_bulk_insert_rejects_mismatching_tenant_before_sql(scoped_engine) -> None:
    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        statements.clear()
        with pytest.raises(ValueError, match="tenant ownership"):
            scoped.execute(insert(Capability).values(id="cap-b", tenant_id="tenant-b", name="B"))

    assert statements == []


@pytest.mark.parametrize("tenant_id", [None, func.lower("TENANT-A")], ids=["null", "expression"])
def test_mapped_bulk_insert_rejects_explicit_untrusted_ownership_before_sql(
    scoped_engine, tenant_id
) -> None:
    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        statements.clear()
        with pytest.raises(ValueError, match="tenant ownership"):
            scoped.execute(insert(Capability).values(id="cap-a", tenant_id=tenant_id, name="A"))

    assert statements == []


def test_mapped_executemany_insert_fills_and_validates_entire_batch(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        scoped.execute(
            insert(Capability),
            [{"id": "cap-a", "name": "A"}, {"id": "cap-b", "name": "B"}],
        )
        scoped.commit()

    with Session(scoped_engine) as raw:
        tenant_ids = raw.scalars(select(Capability.tenant_id).order_by(Capability.id)).all()
    assert tenant_ids == ["tenant-a", "tenant-a"]

    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        statements.clear()
        with pytest.raises(ValueError, match="tenant ownership"):
            scoped.execute(
                insert(Capability),
                [
                    {"id": "cap-c", "name": "C"},
                    {"id": "cap-d", "tenant_id": "tenant-b", "name": "D"},
                ],
            )

    assert statements == []


def test_unmapped_core_insert_cannot_bypass_the_scoped_gateway(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        with pytest.raises(ValueError, match="mapped resource"):
            scoped.execute(insert(Capability.__table__).values(id="cap-a", name="A"))


def test_unmapped_sql_cannot_bypass_the_scoped_gateway(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        with pytest.raises(ValueError, match="mapped resource"):
            scoped.execute(text("SELECT * FROM capabilities"))


@pytest.mark.parametrize(
    "statement",
    [
        select(Capability).from_statement(text("SELECT * FROM capabilities")),
        select(Capability, Capability.__table__.c.tenant_id),
        select(
            Capability.id,
            select(func.count()).select_from(Capability.__table__).scalar_subquery(),
        ),
    ],
    ids=["from-statement", "mixed-core-column", "nested-core-table"],
)
def test_tenant_select_rejects_unscopable_statement_shapes_before_sql(
    scoped_engine, statement
) -> None:
    _seed_capabilities(scoped_engine)
    statements: list[str] = []
    event.listen(
        scoped_engine,
        "before_cursor_execute",
        lambda _conn, _cursor, sql, _params, _context, _many: statements.append(sql),
    )

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        statements.clear()
        with pytest.raises(ValueError, match="scopable ORM SELECT"):
            scoped.execute(statement)

    assert statements == []


def test_nested_orm_scalar_subquery_is_tenant_scoped(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)
    nested_capability = aliased(Capability)
    tenant_count = select(func.count(nested_capability.id)).scalar_subquery()

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        rows = scoped.execute(select(Capability.id, tenant_count)).all()

    assert rows == [("cap-a", 1)]


def test_execute_returns_only_restrictive_result_facades(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        result = scoped.execute(select(Capability).order_by(Capability.id))
        for escape_name in ("context", "connection", "root_connection", "raw"):
            assert not hasattr(result, escape_name)

        scalar_result = result.scalars()
        assert [row.id for row in scalar_result.all()] == ["cap-a"]
        for escape_name in ("context", "connection", "root_connection", "raw"):
            assert not hasattr(scalar_result, escape_name)

        dml_result = scoped.execute(
            update(Capability).where(Capability.id == "cap-a").values(name="updated")
        )
        assert not hasattr(dml_result, "context")


def test_get_does_not_return_foreign_identity_preloaded_before_binding(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as foreign_session:
        foreign = foreign_session.get(Capability, "cap-b")
        assert foreign is not None
        foreign_session.expunge(foreign)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        raw.add(foreign)

        assert scoped.get(Capability, "cap-b") is None


def test_refresh_rejects_foreign_identity_preloaded_before_binding(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)

    with Session(scoped_engine) as foreign_session:
        foreign = foreign_session.get(Capability, "cap-b")
        assert foreign is not None
        foreign_session.expunge(foreign)

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        raw.add(foreign)

        with pytest.raises(ValueError, match="tenant ownership"):
            scoped.refresh(foreign)


def test_binding_rejects_preloaded_identity_map_and_relationships(scoped_engine) -> None:
    _seed_capabilities(scoped_engine)
    with Session(scoped_engine) as seed:
        seed.add(
            Implementation(
                id="impl-b",
                tenant_id="tenant-b",
                capability_id="cap-b",
                name="foreign implementation",
            )
        )
        seed.commit()

    with Session(scoped_engine) as raw:
        foreign = raw.get(Capability, "cap-b")
        assert foreign is not None
        assert [item.id for item in foreign.implementations] == ["impl-b"]

        with pytest.raises(ValueError, match="identity map must be empty"):
            ScopedSession(raw, _scope())


def test_global_model_rejects_tenant_binding(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        with pytest.raises(ValueError, match="global resources"):
            scoped.scalars(select(PricingCatalog)).all()


def test_global_model_rejects_ownership_fields(scoped_engine) -> None:
    row = PricingCatalog(
        provider="provider",
        model="model",
        effective_from=datetime(2026, 8, 11, tzinfo=UTC),
    )
    row.tenant_id = "tenant-a"

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, None)
        scoped.add(row)
        with pytest.raises(ValueError, match="global resources cannot declare ownership"):
            scoped.commit()


def test_workspace_definition_filters_tenant_and_workspace(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        raw.add_all(
            [
                WorkspaceRecord(tenant_id="tenant-a", workspace_id="workspace-a", value="visible"),
                WorkspaceRecord(
                    tenant_id="tenant-a", workspace_id="workspace-b", value="hidden-workspace"
                ),
                WorkspaceRecord(
                    tenant_id="tenant-b", workspace_id="workspace-a", value="hidden-tenant"
                ),
            ]
        )
        raw.commit()

    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, _scope())
        values = scoped.scalars(select(WorkspaceRecord.value)).all()

    assert values == ["visible"]


def test_workspace_definition_rejects_tenant_wide_binding(scoped_engine) -> None:
    with Session(scoped_engine) as raw:
        scoped = ScopedSession(raw, TenantWideScopeContext(tenant_id="tenant-a"))
        with pytest.raises(ValueError, match="workspace-scoped resources"):
            scoped.scalars(select(WorkspaceRecord)).all()


def test_sqlite_compat_adds_tenant_ownership_to_existing_operational_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_engine = create_engine("sqlite://", future=True)
    tables = {
        "users",
        "deployment_implementations",
        "connector_delivery_log",
        "cost_profiles",
        "cost_estimates",
        "ground_truth_costs",
        "calibration_metrics",
        "dashboard_views",
        "enforcement_actions",
        "traffic_policies",
        "budget_policies",
        "audit_log",
    }
    with legacy_engine.begin() as connection:
        for table in tables:
            connection.execute(text(f"CREATE TABLE {table} (legacy_key INTEGER)"))
    monkeypatch.setattr(database, "engine", legacy_engine)

    database._ensure_sqlite_compat()

    for table in tables:
        assert "tenant_id" in {
            column["name"] for column in inspect(legacy_engine).get_columns(table)
        }
