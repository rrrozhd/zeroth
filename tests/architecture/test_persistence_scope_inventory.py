from __future__ import annotations

import ast
import fnmatch
import importlib
import os
import pkgutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from zeroth.econ.plane import __path__ as econ_plane_paths
from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
    ScopeContext,
)
from zeroth.platform.storage.scoped_table import (
    ASYNC_PERSISTENCE_MODULES,
    ASYNC_NON_PERSISTENCE_MODULES,
    ECON_MIGRATION_SCOPE_DEFINITIONS,
    SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES,
    SERVICE_SCOPE_DEFINITIONS,
    ScopedTable,
)
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase

_ECON_PLANE_ROOT = Path(econ_plane_paths[0])
_SOURCE_ROOT = _ECON_PLANE_ROOT.parents[1]
_GLOBAL_TABLES = {"pricing_catalog", "tool_pricing_catalog", "roles", "user_roles"}


@pytest.fixture(scope="module")
def migration_head_tables(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[set[str], set[str], dict[str, set[str]]]:
    root = Path(__file__).resolve().parents[2]

    service_path = tmp_path_factory.mktemp("service-scope-head") / "service.db"
    service_url = f"sqlite:///{service_path}"
    service_config = Config()
    service_config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    service_config.set_main_option("sqlalchemy.url", service_url)
    command.upgrade(service_config, "head")

    econ_path = tmp_path_factory.mktemp("econ-scope-head") / "econ.db"
    econ_url = f"sqlite:///{econ_path}"
    econ_config = Config()
    econ_config.set_main_option("script_location", str(root / "src/zeroth/econ/plane/_migrations"))
    econ_config.set_main_option("sqlalchemy.url", econ_url)
    previous_econ_url = os.environ.get("ECP_DATABASE_URL")
    os.environ["ECP_DATABASE_URL"] = econ_url
    try:
        command.upgrade(econ_config, "head")
    finally:
        if previous_econ_url is None:
            os.environ.pop("ECP_DATABASE_URL", None)
        else:
            os.environ["ECP_DATABASE_URL"] = previous_econ_url

    def schema(url: str) -> tuple[set[str], dict[str, set[str]]]:
        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            columns = {
                table: {column["name"] for column in inspector.get_columns(table)}
                for table in tables
            }
            return tables, columns
        finally:
            engine.dispose()

    service_tables, service_columns = schema(service_url)
    econ_tables, _ = schema(econ_url)
    return service_tables, econ_tables, service_columns


def _import_econ_model_modules() -> None:
    for module in pkgutil.walk_packages(econ_plane_paths, prefix="zeroth.econ.plane."):
        if module.name.endswith(".models"):
            importlib.import_module(module.name)


def _econ_mapper_classes() -> list[type]:
    _import_econ_model_modules()
    return sorted(
        (
            mapper.class_
            for mapper in Base.registry.mappers
            if mapper.class_.__module__.startswith("zeroth.econ.plane.")
            and mapper.class_.__module__.endswith(".models")
        ),
        key=lambda cls: cls.__tablename__,
    )


def test_every_econ_mapper_has_exactly_one_valid_scope_definition() -> None:
    definitions: list[ResourceScopeDefinition] = []
    for model in _econ_mapper_classes():
        declared = vars(model).get("scope_definition")
        assert type(declared) is ResourceScopeDefinition, model
        assert declared.table_name == model.__tablename__, model
        assert declared.operations == frozenset(ResourceOperation), model
        definitions.append(declared)

    registry = ResourceScopeRegistry(definitions)
    assert len(registry.definitions) == len(_econ_mapper_classes())


def test_econ_scope_classifications_match_product_semantics() -> None:
    definitions = {model.__tablename__: model.scope_definition for model in _econ_mapper_classes()}

    assert {
        name for name, item in definitions.items() if item.scope is ResourceScope.GLOBAL
    } == _GLOBAL_TABLES
    for model in _econ_mapper_classes():
        definition = definitions[model.__tablename__]
        columns = inspect(model).columns
        if definition.scope is ResourceScope.TENANT_SCOPED:
            assert "tenant_id" in columns, model
        else:
            assert "tenant_id" not in columns, model
        if definition.workspace_scoped:
            assert "workspace_id" in columns, model

    assert definitions["users"].scope is ResourceScope.TENANT_SCOPED


def test_service_migration_head_has_exactly_one_production_scope_definition(
    migration_head_tables: tuple[set[str], set[str], dict[str, set[str]]],
) -> None:
    service_tables, _, _ = migration_head_tables
    registry = ResourceScopeRegistry(SERVICE_SCOPE_DEFINITIONS)

    assert {definition.table_name for definition in registry.definitions} == service_tables
    assert len(registry.definitions) == len(service_tables)
    assert {
        "contract_versions",
        "webhook_subscriptions",
        "webhook_deliveries",
        "webhook_dead_letters",
        "retention_policies",
        "retention_audit_log",
        "retention_cleanup_state",
        "retention_cleanup_operations",
        "retention_coordination",
        "legal_holds",
        "langgraph_decisions",
        "langgraph_inventories",
        "langgraph_run_attestations",
    } <= service_tables
    assert {
        definition.table_name
        for definition in registry.definitions
        if definition.scope is ResourceScope.GLOBAL
    } == {"alembic_version", "schema_versions"}


def test_econ_migration_head_reuses_mapper_definitions_without_duplicates(
    migration_head_tables: tuple[set[str], set[str], dict[str, set[str]]],
) -> None:
    _, econ_tables, _ = migration_head_tables
    mapper_definitions = [model.scope_definition for model in _econ_mapper_classes()]
    registry = ResourceScopeRegistry([*mapper_definitions, *ECON_MIGRATION_SCOPE_DEFINITIONS])

    assert econ_tables <= {definition.table_name for definition in registry.definitions}
    for table_name in econ_tables - {
        "alembic_version",
        "_zeroth_20260811_04_auth_scope",
    }:
        assert registry.definition_for_table(table_name) == next(
            definition for definition in mapper_definitions if definition.table_name == table_name
        )
    assert {definition.table_name for definition in ECON_MIGRATION_SCOPE_DEFINITIONS} == {
        "alembic_version",
        "_zeroth_20260811_04_auth_scope",
    }


def test_service_workspace_scope_definitions_match_head_columns(
    migration_head_tables: tuple[set[str], set[str], dict[str, set[str]]],
) -> None:
    _, _, service_columns = migration_head_tables
    workspace_tables = {
        definition.table_name
        for definition in SERVICE_SCOPE_DEFINITIONS
        if definition.workspace_scoped
    }
    assert workspace_tables == {
        "approvals",
        "deployment_versions",
        "graph_versions",
        "node_audits",
        "run_checkpoints",
        "runs",
        "threads",
    }
    assert all("workspace_id" in service_columns[table] for table in workspace_tables)


def test_service_direct_ownership_matches_live_head_and_pending_is_not_bindable(
    migration_head_tables: tuple[set[str], set[str], dict[str, set[str]]],
    tmp_path: Path,
) -> None:
    _, _, service_columns = migration_head_tables
    tenant_definitions = {
        definition.table_name: definition
        for definition in SERVICE_SCOPE_DEFINITIONS
        if definition.scope is ResourceScope.TENANT_SCOPED
    }
    missing_direct_tenant = {
        table_name
        for table_name in tenant_definitions
        if "tenant_id" not in service_columns[table_name]
    }

    assert missing_direct_tenant == SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES
    for table_name, definition in tenant_definitions.items():
        assert definition.direct_scope_ready is (
            table_name not in SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES
        )
        if definition.direct_scope_ready:
            assert "tenant_id" in service_columns[table_name]

    registry = ResourceScopeRegistry(SERVICE_SCOPE_DEFINITIONS)
    database = AsyncSQLiteDatabase(str(tmp_path / "pending-scope.db"))
    context = ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a")
    for table_name in SERVICE_PENDING_DIRECT_OWNERSHIP_TABLES:
        definition = registry.definition_for_table(table_name)
        with pytest.raises(ValueError, match="pending direct ownership"):
            ScopedTable(database, registry, definition.resource_name, context)


class _RawSessionVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.function = "<module>"
        self.violations: list[str] = []
        self._counts: dict[tuple[str, str], int] = {}
        self._session_names: set[str] = set()
        self._sessionmaker_names: set[str] = set()
        self._factory_names: set[str] = set()
        self._orm_module_aliases: set[str] = set()
        self._database_module_aliases: set[str] = set()
        self._session_receiver_names: set[str] = set()
        self._tainted_result_names: set[str] = set()

    def _record(self, kind: str, detail: str) -> None:
        key = (kind, f"{self.function}:{detail}")
        ordinal = self._counts.get(key, 0) + 1
        self._counts[key] = ordinal
        self.violations.append(f"{self.relative_path}::{kind}::{key[1]}#{ordinal}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        previous_taint = self._tainted_result_names
        previous_receivers = self._session_receiver_names
        self.function = node.name
        self._tainted_result_names = set()
        self._session_receiver_names = {
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if argument.arg in {"db", "raw_session", "session"}
            or self._is_session_reference(argument.annotation)
        }
        self.generic_visit(node)
        self.function = previous
        self._tainted_result_names = previous_taint
        self._session_receiver_names = previous_receivers

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "sqlalchemy":
            for alias in node.names:
                if alias.name == "orm":
                    self._orm_module_aliases.add(alias.asname or alias.name)
                    self._record("import", "sqlalchemy.orm")
        if node.module == "sqlalchemy.orm":
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name == "Session":
                    self._session_names.add(local_name)
                    self._record("import", "sqlalchemy.orm.Session")
                elif alias.name == "sessionmaker":
                    self._sessionmaker_names.add(local_name)
                    self._record("factory", "sqlalchemy.orm.sessionmaker")
        if node.module == "zeroth.econ.plane.database":
            for alias in node.names:
                if alias.name == "SessionLocal":
                    self._factory_names.add(alias.asname or alias.name)
                    self._record("factory", "zeroth.econ.plane.database.SessionLocal")
        if node.module == "zeroth.econ.plane":
            for alias in node.names:
                if alias.name == "database":
                    self._database_module_aliases.add(alias.asname or alias.name)
                    self._record("factory", "zeroth.econ.plane.database.SessionLocal")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "sqlalchemy.orm":
                self._orm_module_aliases.add(alias.asname or alias.name)
                self._record("import", "sqlalchemy.orm")
            elif alias.name == "zeroth.econ.plane.database":
                self._database_module_aliases.add(alias.asname or alias.name)
                self._record("factory", "zeroth.econ.plane.database.SessionLocal")
        self.generic_visit(node)

    @staticmethod
    def _dotted_name(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _RawSessionVisitor._dotted_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else None
        return None

    @staticmethod
    def _assigned_names(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            return {
                name
                for element in node.elts
                for name in _RawSessionVisitor._assigned_names(element)
            }
        return set()

    @staticmethod
    def _assigned_result_paths(node: ast.AST) -> set[str]:
        if isinstance(node, (ast.Name, ast.Attribute)):
            dotted = _RawSessionVisitor._dotted_name(node)
            return {dotted} if dotted else set()
        if isinstance(node, (ast.Tuple, ast.List)):
            return {
                name
                for element in node.elts
                for name in _RawSessionVisitor._assigned_result_paths(element)
            }
        return set()

    def _is_sessionmaker_call(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        dotted = self._dotted_name(node.func)
        return dotted in self._sessionmaker_names or any(
            dotted == f"{module}.sessionmaker" for module in self._orm_module_aliases
        )

    def _is_session_reference(self, node: ast.AST | None) -> bool:
        if not isinstance(node, ast.expr):
            return False
        dotted = self._dotted_name(node)
        return dotted in self._session_names or any(
            dotted == f"{module}.Session" for module in self._orm_module_aliases
        )

    def _is_factory_reference(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.expr):
            return False
        dotted = self._dotted_name(node)
        return dotted in self._factory_names or any(
            dotted == f"{module}.SessionLocal" for module in self._database_module_aliases
        )

    def _is_session_constructor_call(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and self._is_session_reference(node.func)

    def _is_factory_call(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        return self._is_factory_reference(node.func) or (
            isinstance(node.func, ast.Call) and self._is_sessionmaker_call(node.func)
        )

    def _is_session_receiver(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self._session_receiver_names
        if isinstance(node, ast.Attribute):
            dotted = self._dotted_name(node)
            return dotted in self._session_receiver_names if dotted is not None else False
        return self._is_session_constructor_call(node) or self._is_factory_call(node)

    def _is_tainted_result(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self._tainted_result_names
        if isinstance(node, ast.Attribute):
            dotted = self._dotted_name(node)
            return (
                dotted in self._tainted_result_names if dotted is not None else False
            ) or self._is_tainted_result(node.value)
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if node.func.attr == "execute" and self._is_session_receiver(receiver):
                return True
            if node.func.attr == "scalars" and (
                self._is_session_receiver(receiver) or self._is_tainted_result(receiver)
            ):
                return True
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and self._is_tainted_result(node.args[0])
        ):
            return True
        return self._is_tainted_result(node.func)

    def _update_result_taint(self, targets: list[ast.expr], value: ast.AST) -> None:
        assigned = {name for target in targets for name in self._assigned_result_paths(target)}
        if self._is_tainted_result(value):
            self._tainted_result_names.update(assigned)
        else:
            self._tainted_result_names.difference_update(assigned)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_sessionmaker_call(node.value) or self._is_factory_reference(node.value):
            for target in node.targets:
                self._factory_names.update(self._assigned_names(target))
        assigned_receivers = {
            name for target in node.targets for name in self._assigned_result_paths(target)
        }
        if self._is_session_receiver(node.value):
            self._session_receiver_names.update(assigned_receivers)
        else:
            self._session_receiver_names.difference_update(assigned_receivers)
        self._update_result_taint(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self._is_sessionmaker_call(node.value):
            self._factory_names.update(self._assigned_names(node.target))
        if node.value is not None:
            assigned_receivers = self._assigned_result_paths(node.target)
            if self._is_session_receiver(node.value):
                self._session_receiver_names.update(assigned_receivers)
            else:
                self._session_receiver_names.difference_update(assigned_receivers)
            self._update_result_taint([node.target], node.value)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self._session_names and not isinstance(node.ctx, ast.Store):
            parent = getattr(node, "_scope_guard_parent", None)
            if not isinstance(parent, (ast.alias, ast.ImportFrom)):
                self._record("reference", node.id)
        if node.id in self._factory_names and not isinstance(node.ctx, ast.Store):
            self._record("factory", node.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        dotted = self._dotted_name(node.func)
        if self._is_session_reference(node.func):
            self._record("construction", dotted or "Session")
        if self._is_factory_reference(node.func):
            self._record("construction", dotted)
        if self._is_sessionmaker_call(node):
            self._record("factory", dotted or "sessionmaker")
        if isinstance(node.func, ast.Call) and self._is_sessionmaker_call(node.func):
            self._record("construction", "sessionmaker-result")
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and self._is_tainted_result(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"connection", "context", "raw", "root_connection"}
        ):
            self._record("raw-access", str(node.args[1].value))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        tainted_escape = node.attr in {
            "connection",
            "context",
            "raw",
            "root_connection",
        } and self._is_tainted_result(node.value)
        if tainted_escape or (
            node.attr in {"bind", "connection", "get_bind", "query"}
            and self._is_session_receiver(node.value)
        ):
            self._record("raw-access", node.attr)
        self.generic_visit(node)


def _raw_session_violations(root: Path) -> set[str]:
    violations: set[str] = set()
    for path in sorted(root.rglob("service.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._scope_guard_parent = parent
        visitor = _RawSessionVisitor(path.relative_to(root).as_posix())
        visitor.visit(tree)
        violations.update(visitor.violations)
    return violations


# Remaining service signatures stay on the shrink-only compatibility boundary
# until their owning ZER-44 migration task converts them to ScopedSession.
# This exact allowlist is shrink-only: deleting a violation requires deleting its entry,
# while any new import, reference, construction, or raw-only API use fails this test.
_ALLOWED_SESSION_REFERENCES = {
    "capabilities/service.py": {
        "active_experiment",
        "create_capability",
        "create_deployment",
        "create_experiment",
        "create_implementation",
        "deployment_active_impl_ids",
        "get_capability",
        "get_deployment",
        "get_implementation",
        "list_capabilities",
        "list_experiments",
    },
    "costing/service.py": {
        "_lookup_pricing",
        "create_cost_profile",
        "create_pricing_catalog",
        "estimate_cost_for_period",
        "get_cost_profile",
        "latest_cost_estimate",
    },
    "dashboard/service.py": {
        "action_suppression",
        "calibration_trend",
        "capability_ranking",
        "capital_destroyers",
        "confidence_gate_status",
        "confidence_trend",
        "data_quality_mix",
        "drift_timeline",
        "efficiency_trend",
        "estimated_vs_ground_truth_cost",
        "implementation_compare",
        "kpis",
        "policy_timeline",
        "top_creators",
    },
    "enforcement/service.py": {
        "_apply_traffic_policy",
        "_propose_policy_action",
        "create_action",
        "decide_action",
        "get_budget_status",
        "list_actions",
        "list_policy_actions",
        "upsert_tenant_budget",
    },
    "performance/service.py": {"calculate_snapshots", "latest_snapshots"},
}
_RAW_SESSION_ALLOWLIST = (
    {f"{path}::import::<module>:sqlalchemy.orm.Session#1" for path in _ALLOWED_SESSION_REFERENCES}
    | {
        f"{path}::reference::{function}:Session#1"
        for path, functions in _ALLOWED_SESSION_REFERENCES.items()
        for function in functions
    }
    | {"capabilities/service.py::raw-access::create_deployment:query#1"}
)


def test_raw_session_allowlist_is_exact_and_shrink_only() -> None:
    assert _raw_session_violations(_ECON_PLANE_ROOT) == _RAW_SESSION_ALLOWLIST


def test_raw_session_guard_reports_a_new_scoped_service_violation(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "from sqlalchemy.orm import Session\n\n"
        "def unsafe(db: Session):\n"
        "    result = db.query(object).all()\n"
        "    return result.context.root_connection\n"
    )

    violations = _raw_session_violations(tmp_path)

    assert any("::import::" in violation for violation in violations)
    assert any("::reference::" in violation for violation in violations)
    assert any("::raw-access::" in violation for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy.orm\n\ndef unsafe():\n    return sqlalchemy.orm.Session()\n",
        "from sqlalchemy.orm import Session as OrmSession\n\ndef unsafe():\n    return OrmSession()\n",
        (
            "from sqlalchemy.orm import sessionmaker as make_session\n\n"
            "RawFactory = make_session()\n\n"
            "def unsafe():\n    return RawFactory()\n"
        ),
        (
            "from zeroth.econ.plane.database import SessionLocal as RawFactory\n\n"
            "def unsafe():\n    return RawFactory()\n"
        ),
    ],
    ids=["module-session", "aliased-session", "sessionmaker", "session-local"],
)
def test_raw_session_guard_tracks_import_aliases_and_factories(tmp_path: Path, source: str) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(source)

    violations = _raw_session_violations(tmp_path)

    assert any("construction" in violation or "factory" in violation for violation in violations)


def test_raw_session_guard_does_not_flag_unrelated_request_context(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "def safe(request):\n"
        "    return request.context.connection, request.root_connection, request.raw\n"
    )

    assert _raw_session_violations(tmp_path) == set()


def test_raw_session_guard_tracks_result_dataflow_and_getattr(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "def unsafe(db, statement):\n"
        "    obscure_name = db.execute(statement)\n"
        "    alias = obscure_name\n"
        "    scalar_alias = alias.scalars()\n"
        "    leaked_context = getattr(scalar_alias, 'context')\n"
        "    return leaked_context.connection.root_connection\n"
    )

    violations = _raw_session_violations(tmp_path)

    assert any("context" in violation for violation in violations)
    assert any("connection" in violation for violation in violations)
    assert any("root_connection" in violation for violation in violations)


def test_raw_session_guard_tracks_attribute_assignment_result_taint(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "def unsafe(db, statement, holder):\n"
        "    holder.result = db.execute(statement)\n"
        "    return holder.result.context.connection\n"
    )

    violations = _raw_session_violations(tmp_path)

    assert any("context" in violation for violation in violations)
    assert any("connection" in violation for violation in violations)


def test_raw_session_guard_tracks_qualified_session_local_factory(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "import zeroth.econ.plane.database as database\n\n"
        "def unsafe():\n"
        "    return database.SessionLocal()\n"
    )

    violations = _raw_session_violations(tmp_path)

    assert any("construction" in violation for violation in violations)


def test_raw_session_guard_tracks_package_imported_database_factory(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "from zeroth.econ.plane import database\n\n"
        "def unsafe():\n"
        "    return database.SessionLocal()\n"
    )

    violations = _raw_session_violations(tmp_path)

    assert any("construction" in violation for violation in violations)


def test_raw_session_guard_tracks_sqlalchemy_package_orm_alias(tmp_path: Path) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(
        "from sqlalchemy import orm as saorm\n\ndef unsafe():\n    return saorm.Session()\n"
    )

    violations = _raw_session_violations(tmp_path)

    assert any("construction" in violation for violation in violations)


@pytest.mark.parametrize(
    "source",
    [
        "class Session:\n    pass\n\ndef safe(value: Session):\n    return value\n",
        "SessionLocal = lambda: object()\n\ndef safe():\n    return SessionLocal()\n",
        (
            "def safe(client, statement):\n"
            "    rows = client.execute(statement)\n"
            "    return rows.context\n"
        ),
    ],
    ids=["local-session", "unrelated-session-local", "non-session-execute"],
)
def test_raw_session_guard_ignores_unrelated_symbols_and_results(
    tmp_path: Path, source: str
) -> None:
    service_dir = tmp_path / "new_feature"
    service_dir.mkdir()
    (service_dir / "service.py").write_text(source)

    assert _raw_session_violations(tmp_path) == set()


class _RawAsyncRepositoryVisitor(ast.NodeVisitor):
    _RAW_METHODS = {"execute", "execute_script", "fetch_all", "fetch_one", "transaction"}
    _CONNECTION_METHODS = _RAW_METHODS - {"transaction"}
    _CONNECTION_CONTEXT_METHODS = {"acquire", "connect", "connection", "transaction"}
    _DATABASE_RECEIVER_NAMES = {
        "_coordinator",
        "_database",
        "coordinator",
        "database",
        "db",
        "pool",
    }
    _POTENTIAL_CONNECTION_NAMES = {
        "_conn",
        "_connection",
        "conn",
        "connection",
        "raw",
        "transaction",
    }

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.function = "<module>"
        self.violations: list[str] = []
        self._counts: dict[tuple[str, str], int] = {}
        self._connection_paths: set[str] = set()
        self._database_paths: set[str] = set()
        self._transaction_factory_paths: set[str] = set()

    def _record(self, method: str) -> None:
        key = (self.function, method)
        ordinal = self._counts.get(key, 0) + 1
        self._counts[key] = ordinal
        self.violations.append(f"{self.relative_path}::{self.function}::{method}#{ordinal}")

    @staticmethod
    def _dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _RawAsyncRepositoryVisitor._dotted_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else None
        return None

    @classmethod
    def _assigned_paths(cls, node: ast.AST) -> set[str]:
        if isinstance(node, (ast.Name, ast.Attribute)):
            dotted = cls._dotted_name(node)
            return {dotted} if dotted else set()
        if isinstance(node, (ast.Tuple, ast.List)):
            return {path for item in node.elts for path in cls._assigned_paths(item)}
        return set()

    @classmethod
    def _is_connection_annotation(cls, node: ast.AST | None) -> bool:
        return cls._dotted_name(node) in {
            "AsyncConnection",
            "storage.AsyncConnection",
            "zeroth.platform.storage.AsyncConnection",
            "zeroth.platform.storage.database.AsyncConnection",
        }

    @classmethod
    def _is_database_annotation(cls, node: ast.AST | None) -> bool:
        dotted = cls._dotted_name(node)
        return dotted is not None and dotted.endswith("AsyncDatabase")

    @classmethod
    def _seed_connection_argument(cls, argument: ast.arg) -> set[str]:
        if argument.arg in cls._POTENTIAL_CONNECTION_NAMES or cls._is_connection_annotation(
            argument.annotation
        ):
            return {argument.arg}
        annotation = cls._dotted_name(argument.annotation)
        if annotation is not None and annotation.endswith("Transaction"):
            return {f"{argument.arg}.connection"}
        return set()

    def _is_database_receiver(self, node: ast.AST) -> bool:
        dotted = self._dotted_name(node)
        if dotted is None:
            return False
        leaf = dotted.rsplit(".", 1)[-1]
        return leaf in self._DATABASE_RECEIVER_NAMES or any(
            dotted == path or dotted.startswith(f"{path}.") for path in self._database_paths
        )

    def _is_connection_receiver(self, node: ast.AST) -> bool:
        dotted = self._dotted_name(node)
        if dotted is None:
            return False
        leaf = dotted.rsplit(".", 1)[-1]
        return leaf in self._POTENTIAL_CONNECTION_NAMES or any(
            dotted == path or dotted.startswith(f"{path}.") for path in self._connection_paths
        )

    def _is_transaction_factory(self, node: ast.AST) -> bool:
        dotted = self._dotted_name(node)
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "transaction"
            and self._is_database_receiver(node.value)
        ) or (dotted is not None and dotted in self._transaction_factory_paths)

    def _is_connection_context(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if self._is_transaction_factory(node.func):
            return True
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in self._CONNECTION_CONTEXT_METHODS
            and self._is_database_receiver(node.func.value)
        )

    def _is_connection_value(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Await):
            return self._is_connection_value(node.value)
        if self._is_connection_receiver(node):
            return True
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in self._CONNECTION_CONTEXT_METHODS
            and self._is_database_receiver(node.func.value)
        )

    def _update_connection_target(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            for target_item, value_item in zip(target.elts, value.elts, strict=False):
                self._update_connection_target(target_item, value_item)
            return
        paths = self._assigned_paths(target)
        if self._is_connection_value(value):
            self._connection_paths.update(paths)
        else:
            self._connection_paths.difference_update(paths)

    def _update_transaction_factory_target(self, target: ast.AST, value: ast.AST) -> None:
        paths = self._assigned_paths(target)
        if self._is_transaction_factory(value):
            self._transaction_factory_paths.update(paths)
        else:
            self._transaction_factory_paths.difference_update(paths)

    def _update_database_target(self, target: ast.AST, value: ast.AST) -> None:
        paths = self._assigned_paths(target)
        if self._is_database_receiver(value):
            self._database_paths.update(paths)
        else:
            self._database_paths.difference_update(paths)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = (
            self.function,
            self._connection_paths,
            self._database_paths,
            self._transaction_factory_paths,
        )
        self.function = node.name
        self._connection_paths = {
            path
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for path in self._seed_connection_argument(argument)
        }
        self._database_paths = {
            argument.arg
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if argument.arg in {"database", "db"}
            or self._is_database_annotation(argument.annotation)
        }
        self._transaction_factory_paths = set()
        self.generic_visit(node)
        (
            self.function,
            self._connection_paths,
            self._database_paths,
            self._transaction_factory_paths,
        ) = previous

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._update_connection_target(target, node.value)
            self._update_database_target(target, node.value)
            self._update_transaction_factory_target(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._update_connection_target(node.target, node.value)
            self._update_database_target(node.target, node.value)
            if self._is_connection_annotation(node.annotation):
                self._connection_paths.update(self._assigned_paths(node.target))
            self._update_transaction_factory_target(node.target, node.value)
        self.generic_visit(node)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None and self._is_connection_context(item.context_expr):
                self._connection_paths.update(self._assigned_paths(item.optional_vars))
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self._visit_with(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in self._RAW_METHODS
            and (
                (node.args[1].value == "transaction" and self._is_database_receiver(node.args[0]))
                or self._is_connection_receiver(node.args[0])
            )
        ):
            self._record(str(node.args[1].value))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_transaction_factory(node) or (
            node.attr in self._CONNECTION_METHODS and self._is_connection_receiver(node.value)
        ):
            self._record(node.attr)
        self.generic_visit(node)


def _raw_async_repository_violations(
    root: Path,
    modules: frozenset[str] = ASYNC_PERSISTENCE_MODULES,
) -> set[str]:
    violations: set[str] = set()
    for relative_path in sorted(modules):
        path = root / relative_path
        if not path.is_file():
            raise AssertionError(f"registered persistence module is missing: {relative_path}")
        visitor = _RawAsyncRepositoryVisitor(relative_path)
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        violations.update(visitor.violations)
    return violations


_PERSISTENCE_DISCOVERY_ROOTS = (
    "contracts",
    "governance",
    "integrations/memory",
    "integrations/persistence",
    "platform/artifacts",
    "platform/secrets",
    "runtime/agents",
    "service/deployments",
    "service/langgraph_gateway",
    "service/webhooks",
)
_PERSISTENCE_FILENAME_PATTERNS = (
    "repository.py",
    "*_repository.py",
    "store.py",
    "*_store.py",
    "storage.py",
    "registry.py",
)


def _discover_persistence_shaped_modules(root: Path) -> frozenset[str]:
    discovered: set[str] = set()
    for relative_root in _PERSISTENCE_DISCOVERY_ROOTS:
        directory = root / relative_root
        if not directory.exists():
            continue
        for path in directory.rglob("*.py"):
            if any(
                fnmatch.fnmatch(path.name, pattern) for pattern in _PERSISTENCE_FILENAME_PATTERNS
            ):
                discovered.add(path.relative_to(root).as_posix())
    return frozenset(discovered)


# Task 3 records the exact pre-migration debt. Tasks 7-10 delete entries as repositories
# move to ScopedTable; adding a new call or changing an ordinal fails closed.
_RAW_ASYNC_REPOSITORY_ALLOWLIST = frozenset(
    """
contracts/graph/repository.py::_fetch_latest_row::fetch_one#1
contracts/graph/repository.py::_fetch_row::fetch_one#1
contracts/graph/repository.py::_insert_graph::execute#1
contracts/graph/repository.py::_update_graph::execute#1
contracts/graph/repository.py::get::transaction#1
contracts/graph/repository.py::list::fetch_all#1
contracts/graph/repository.py::list::transaction#1
contracts/graph/repository.py::list_versions::fetch_all#1
contracts/graph/repository.py::list_versions::transaction#1
contracts/graph/repository.py::save::transaction#1
governance/approvals/repository.py::get::fetch_one#1
governance/approvals/repository.py::get::transaction#1
governance/approvals/repository.py::list::fetch_all#1
governance/approvals/repository.py::list::transaction#1
governance/approvals/repository.py::list_overdue::fetch_all#1
governance/approvals/repository.py::list_overdue::transaction#1
governance/approvals/repository.py::list_pending::fetch_all#1
governance/approvals/repository.py::list_pending::transaction#1
governance/approvals/repository.py::resolve_pending::fetch_one#1
governance/approvals/repository.py::resolve_pending::transaction#1
governance/approvals/repository.py::write::execute#1
governance/approvals/repository.py::write::transaction#1
governance/audit/repository.py::crypto_erase::transaction#1
governance/audit/repository.py::crypto_erase_in_transaction::execute#1
governance/audit/repository.py::crypto_erase_in_transaction::fetch_one#1
governance/audit/repository.py::get::fetch_one#1
governance/audit/repository.py::get::transaction#1
governance/audit/repository.py::list::fetch_all#1
governance/audit/repository.py::list::transaction#1
governance/audit/repository.py::list_erasable::transaction#1
governance/audit/repository.py::list_erasable_in_transaction::fetch_all#1
governance/audit/repository.py::write::execute#1
governance/audit/repository.py::write::fetch_one#1
governance/audit/repository.py::write::transaction#1
governance/decisions/repository.py::_insert_then_read::execute#1
governance/decisions/repository.py::_insert_then_read::fetch_one#1
governance/decisions/repository.py::_insert_then_read::transaction#1
governance/decisions/repository.py::find_by_idempotency_key::fetch_one#1
governance/decisions/repository.py::find_by_idempotency_key::transaction#1
governance/retention/audit_log_repository.py::get::fetch_one#1
governance/retention/audit_log_repository.py::get::transaction#1
governance/retention/audit_log_repository.py::get_in_transaction::fetch_one#1
governance/retention/audit_log_repository.py::list_for_run::fetch_all#1
governance/retention/audit_log_repository.py::list_for_run::transaction#1
governance/retention/audit_log_repository.py::list_for_run_in_transaction::fetch_all#1
governance/retention/audit_log_repository.py::list_for_tenant::fetch_all#1
governance/retention/audit_log_repository.py::list_for_tenant::transaction#1
governance/retention/audit_log_repository.py::record::transaction#1
governance/retention/audit_log_repository.py::record_in_transaction::execute#1
governance/retention/cleanup_state_repository.py::_update_state_cas::execute#1
governance/retention/cleanup_state_repository.py::get_operation_in_transaction::fetch_one#1
governance/retention/cleanup_state_repository.py::get_state_in_transaction::fetch_one#1
governance/retention/cleanup_state_repository.py::initialize_in_transaction::execute#1
governance/retention/cleanup_state_repository.py::initialize_in_transaction::execute#2
governance/retention/cleanup_state_repository.py::list_operations_in_transaction::fetch_all#1
governance/retention/cleanup_state_repository.py::update_operation_in_transaction::execute#1
governance/retention/legal_hold_repository.py::active_holds_for_tenant::transaction#1
governance/retention/legal_hold_repository.py::active_holds_for_tenant_in_transaction::fetch_all#1
governance/retention/legal_hold_repository.py::get::fetch_one#1
governance/retention/legal_hold_repository.py::get::transaction#1
governance/retention/legal_hold_repository.py::list_for_tenant::fetch_all#1
governance/retention/legal_hold_repository.py::list_for_tenant::transaction#1
governance/retention/legal_hold_repository.py::place::transaction#1
governance/retention/legal_hold_repository.py::place_in_transaction::execute#1
governance/retention/legal_hold_repository.py::release::fetch_one#1
governance/retention/legal_hold_repository.py::release::transaction#1
governance/retention/legal_hold_repository.py::release::transaction#2
governance/retention/legal_hold_repository.py::release_in_transaction::execute#1
governance/retention/legal_hold_repository.py::release_in_transaction::fetch_one#1
governance/retention/policy_repository.py::get::fetch_one#1
governance/retention/policy_repository.py::get::transaction#1
governance/retention/policy_repository.py::list_all_enabled::fetch_all#1
governance/retention/policy_repository.py::list_all_enabled::transaction#1
governance/retention/policy_repository.py::upsert::execute#1
governance/retention/policy_repository.py::upsert::fetch_one#1
governance/retention/policy_repository.py::upsert::transaction#1
integrations/memory/config_repository.py::delete::execute#1
integrations/memory/config_repository.py::delete::fetch_one#1
integrations/memory/config_repository.py::delete::transaction#1
integrations/memory/config_repository.py::get::fetch_one#1
integrations/memory/config_repository.py::get::transaction#1
integrations/memory/config_repository.py::list::fetch_all#1
integrations/memory/config_repository.py::list::transaction#1
integrations/memory/config_repository.py::upsert::execute#1
integrations/memory/config_repository.py::upsert::execute#2
integrations/memory/config_repository.py::upsert::fetch_one#1
integrations/memory/config_repository.py::upsert::transaction#1
integrations/persistence/runs/run_repository.py::_put_run_fenced::fetch_one#1
integrations/persistence/runs/run_repository.py::_put_run_fenced::transaction#1
integrations/persistence/runs/run_repository.py::_save_run_in_connection::fetch_one#1
integrations/persistence/runs/run_repository.py::_save_thread_in_connection::fetch_one#1
integrations/persistence/runs/run_repository.py::count_pending::fetch_one#1
integrations/persistence/runs/run_repository.py::count_pending::transaction#1
integrations/persistence/runs/run_repository.py::create_run::fetch_one#1
integrations/persistence/runs/run_repository.py::create_run::transaction#1
integrations/persistence/runs/run_repository.py::create_thread::transaction#1
integrations/persistence/runs/run_repository.py::delete_run::execute#1
integrations/persistence/runs/run_repository.py::delete_run::transaction#1
integrations/persistence/runs/run_repository.py::erase_checkpoints_for_run::transaction#1
integrations/persistence/runs/run_repository.py::get_run::fetch_one#1
integrations/persistence/runs/run_repository.py::get_run::transaction#1
integrations/persistence/runs/run_repository.py::get_thread::fetch_all#1
integrations/persistence/runs/run_repository.py::get_thread::transaction#1
integrations/persistence/runs/run_repository.py::increment_failure_count::execute#1
integrations/persistence/runs/run_repository.py::increment_failure_count::fetch_one#1
integrations/persistence/runs/run_repository.py::increment_failure_count::transaction#1
integrations/persistence/runs/run_repository.py::list_dead_letter_runs::fetch_all#1
integrations/persistence/runs/run_repository.py::list_dead_letter_runs::transaction#1
integrations/persistence/runs/run_repository.py::list_erasable_run_ids::transaction#1
integrations/persistence/runs/run_repository.py::list_runs::fetch_all#1
integrations/persistence/runs/run_repository.py::list_runs::fetch_all#2
integrations/persistence/runs/run_repository.py::list_runs::transaction#1
integrations/persistence/runs/run_repository.py::list_runs::transaction#2
integrations/persistence/runs/run_repository.py::redact_run::transaction#1
integrations/persistence/runs/run_repository.py::save_run::transaction#1
integrations/persistence/runs/run_repository.py::save_thread::transaction#1
integrations/persistence/runs/thread_repository.py::list::fetch_all#1
integrations/persistence/runs/thread_repository.py::list::transaction#1
service/deployments/repository.py::create::execute#1
service/deployments/repository.py::create::execute#2
service/deployments/repository.py::create::fetch_one#1
service/deployments/repository.py::create::transaction#1
service/deployments/repository.py::get::fetch_one#1
service/deployments/repository.py::get::transaction#1
service/deployments/repository.py::list::fetch_all#1
service/deployments/repository.py::list::transaction#1
service/deployments/repository.py::next_version::fetch_one#1
service/deployments/repository.py::next_version::transaction#1
service/webhooks/repository.py::claim_pending_delivery::execute#1
service/webhooks/repository.py::claim_pending_delivery::fetch_one#1
service/webhooks/repository.py::claim_pending_delivery::transaction#1
service/webhooks/repository.py::create_subscription::execute#1
service/webhooks/repository.py::create_subscription::transaction#1
service/webhooks/repository.py::deactivate_subscription::execute#1
service/webhooks/repository.py::deactivate_subscription::transaction#1
service/webhooks/repository.py::dead_letter::execute#1
service/webhooks/repository.py::dead_letter::execute#2
service/webhooks/repository.py::dead_letter::fetch_one#1
service/webhooks/repository.py::dead_letter::transaction#1
service/webhooks/repository.py::delete_subscription::execute#1
service/webhooks/repository.py::delete_subscription::transaction#1
service/webhooks/repository.py::enqueue_delivery::execute#1
service/webhooks/repository.py::enqueue_delivery::transaction#1
service/webhooks/repository.py::get_dead_letter::fetch_one#1
service/webhooks/repository.py::get_dead_letter::transaction#1
service/webhooks/repository.py::get_subscription::fetch_one#1
service/webhooks/repository.py::get_subscription::transaction#1
service/webhooks/repository.py::list_dead_letters::fetch_all#1
service/webhooks/repository.py::list_dead_letters::transaction#1
service/webhooks/repository.py::list_subscriptions::fetch_all#1
service/webhooks/repository.py::list_subscriptions::transaction#1
service/webhooks/repository.py::list_subscriptions_for_event::fetch_all#1
service/webhooks/repository.py::list_subscriptions_for_event::transaction#1
service/webhooks/repository.py::mark_delivered::execute#1
service/webhooks/repository.py::mark_delivered::transaction#1
service/webhooks/repository.py::mark_failed::execute#1
service/webhooks/repository.py::mark_failed::fetch_one#1
service/webhooks/repository.py::mark_failed::transaction#1
""".split()  # noqa: SIM905 - readable exact snapshot
)

_RAW_ASYNC_REPOSITORY_ALLOWLIST |= frozenset(
    """
governance/attestations/store.py::find_by_correlation::fetch_one#1
governance/attestations/store.py::find_by_correlation::transaction#1
governance/attestations/store.py::find_for_deployment::fetch_one#1
governance/attestations/store.py::find_for_deployment::transaction#1
governance/attestations/store.py::latest_for_deployment::fetch_one#1
governance/attestations/store.py::latest_for_deployment::transaction#1
governance/attestations/store.py::record::execute#1
governance/attestations/store.py::record::fetch_one#1
governance/attestations/store.py::record::transaction#1
governance/attestations/store.py::register::execute#1
governance/attestations/store.py::register::transaction#1
governance/audit/coordination.py::_fetch_legacy_records::fetch_all#1
governance/audit/coordination.py::_fetch_sequenced_records::fetch_all#1
governance/audit/coordination.py::_has_legacy_records::fetch_one#1
governance/audit/coordination.py::advance_audit_chain::execute#1
governance/retention/claims.py::record_heartbeat::transaction#1
governance/retention/claims.py::record_operation_delta::transaction#1
governance/retention/claims.py::record_terminal::transaction#1
governance/retention/claims.py::release::transaction#1
governance/retention/coordination.py::transaction::transaction#1
integrations/persistence/runs/checkpoint_store.py::delete::fetch_one#1
integrations/persistence/runs/checkpoint_store.py::delete::transaction#1
integrations/persistence/runs/checkpoint_store.py::get::fetch_all#1
integrations/persistence/runs/checkpoint_store.py::get::transaction#1
integrations/persistence/runs/checkpoint_store.py::latest_id_for_run::fetch_one#1
integrations/persistence/runs/checkpoint_store.py::latest_id_for_run::transaction#1
integrations/persistence/runs/checkpoint_store.py::list_ids::fetch_all#1
integrations/persistence/runs/checkpoint_store.py::list_ids::transaction#1
integrations/persistence/runs/checkpoint_store.py::write_row::transaction#1
integrations/persistence/runs/checkpoint_store.py::write_row_in_connection::execute#1
integrations/persistence/runs/retention_queries.py::erase_checkpoints_for_run::execute#1
integrations/persistence/runs/retention_queries.py::erase_checkpoints_for_run::fetch_all#1
integrations/persistence/runs/retention_queries.py::erase_token_snapshot_for_run::execute#1
integrations/persistence/runs/retention_queries.py::erase_token_snapshot_for_run::fetch_one#1
integrations/persistence/runs/retention_queries.py::erasure_payloads::fetch_all#1
integrations/persistence/runs/retention_queries.py::erasure_payloads::fetch_one#1
integrations/persistence/runs/retention_queries.py::erasure_payloads::fetch_one#2
integrations/persistence/runs/retention_queries.py::fence_token_snapshot_writes::execute#1
integrations/persistence/runs/retention_queries.py::fence_token_snapshot_writes::fetch_one#1
integrations/persistence/runs/retention_queries.py::lock_and_recheck_erasable_run::fetch_one#1
integrations/persistence/runs/retention_queries.py::redact_run::execute#1
integrations/persistence/runs/retention_queries.py::redact_run::fetch_one#1
integrations/persistence/runs/retention_queries.py::select_erasable_run_ids::fetch_all#1
integrations/persistence/runs/retention_queries.py::tenant_id_for_run::fetch_one#1
integrations/persistence/runs/token_snapshot_store.py::compare_and_swap::fetch_one#1
integrations/persistence/runs/token_snapshot_store.py::compare_and_swap::fetch_one#2
integrations/persistence/runs/token_snapshot_store.py::compare_and_swap::fetch_one#3
integrations/persistence/runs/token_snapshot_store.py::compare_and_swap::fetch_one#4
integrations/persistence/runs/token_snapshot_store.py::compare_and_swap::fetch_one#5
integrations/persistence/runs/token_snapshot_store.py::compare_and_swap::transaction#1
integrations/persistence/runs/token_snapshot_store.py::get::fetch_one#1
integrations/persistence/runs/token_snapshot_store.py::get::transaction#1
service/langgraph_gateway/enforcement_store.py::count_decisions::fetch_one#1
service/langgraph_gateway/enforcement_store.py::count_decisions::transaction#1
service/langgraph_gateway/enforcement_store.py::get_attestation::fetch_all#1
service/langgraph_gateway/enforcement_store.py::get_attestation::transaction#1
service/langgraph_gateway/enforcement_store.py::get_attestation_by_run_id::fetch_one#1
service/langgraph_gateway/enforcement_store.py::get_attestation_by_run_id::transaction#1
service/langgraph_gateway/enforcement_store.py::get_inventory::fetch_one#1
service/langgraph_gateway/enforcement_store.py::get_inventory::transaction#1
service/langgraph_gateway/enforcement_store.py::heartbeat::execute#1
service/langgraph_gateway/enforcement_store.py::heartbeat::fetch_one#1
service/langgraph_gateway/enforcement_store.py::heartbeat::transaction#1
service/langgraph_gateway/enforcement_store.py::register_inventory::execute#1
service/langgraph_gateway/enforcement_store.py::register_inventory::transaction#1
service/langgraph_gateway/enforcement_store.py::save_attestation::execute#1
service/langgraph_gateway/enforcement_store.py::save_attestation::fetch_one#1
service/langgraph_gateway/enforcement_store.py::save_attestation::transaction#1
service/langgraph_gateway/enforcement_store.py::save_decision::execute#1
service/langgraph_gateway/enforcement_store.py::save_decision::fetch_one#1
service/langgraph_gateway/enforcement_store.py::save_decision::transaction#1
""".split()  # noqa: SIM905 - readable exact snapshot
)


def test_raw_async_repository_allowlist_is_exact_and_shrink_only() -> None:
    assert _raw_async_repository_violations(_SOURCE_ROOT) == _RAW_ASYNC_REPOSITORY_ALLOWLIST


_CONTRACT_REGISTRY_BINDING_INVENTORY = frozenset(
    {
        "examples/10_serve_in_python.py::seed_deployment::default_compatibility",
        "examples/20_approval_gate.py::seed_and_build_app::default_compatibility",
        "examples/_common.py::bootstrap_examples_service::default_compatibility",
        "examples/service/seed_deployment.py::main::default_compatibility",
        "src/zeroth/contracts/registry/registry.py::for_scope::scoped",
        "src/zeroth/service/bootstrap/factory.py::bootstrap_service::scoped",
        "src/zeroth/service/bootstrap/factory.py::build_runners_for_deployment::scoped",
        "src/zeroth/service/demo.py::seed_demo::default_compatibility",
    }
)


def _contract_registry_binding_inventory(root: Path) -> frozenset[str]:
    inventory: set[str] = set()
    for search_root in (root / "src", root / "examples"):
        for path in search_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            registry_names = {"ContractRegistry"}
            registry_modules: set[str] = set()
            for imported in ast.walk(tree):
                if isinstance(imported, ast.ImportFrom):
                    for alias in imported.names:
                        if alias.name == "ContractRegistry":
                            registry_names.add(alias.asname or alias.name)
                elif isinstance(imported, ast.Import):
                    for alias in imported.names:
                        if alias.name == "zeroth.contracts.registry":
                            registry_modules.add(alias.asname or alias.name.split(".")[0])
            changed = True
            while changed:
                changed = False
                for assigned in ast.walk(tree):
                    if not isinstance(assigned, ast.Assign) or len(assigned.targets) != 1:
                        continue
                    target = assigned.targets[0]
                    source_is_registry = (
                        isinstance(assigned.value, ast.Name) and assigned.value.id in registry_names
                    ) or (
                        isinstance(assigned.value, ast.Attribute)
                        and assigned.value.attr == "ContractRegistry"
                        and isinstance(assigned.value.value, ast.Name)
                        and assigned.value.value.id in registry_modules
                    )
                    if (
                        isinstance(target, ast.Name)
                        and source_is_registry
                        and target.id not in registry_names
                    ):
                        registry_names.add(target.id)
                        changed = True
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                kind: str | None = None
                registry_receiver = isinstance(node.func, ast.Attribute) and (
                    (isinstance(node.func.value, ast.Name) and node.func.value.id in registry_names)
                    or (
                        isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "ContractRegistry"
                        and isinstance(node.func.value.value, ast.Name)
                        and node.func.value.value.id in registry_modules
                    )
                )
                direct_constructor = (
                    isinstance(node.func, ast.Name) and node.func.id in registry_names
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "ContractRegistry"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in registry_modules
                )
                if direct_constructor:
                    raise AssertionError(
                        f"production legacy ContractRegistry constructor at {path}:{node.lineno}"
                    )
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "for_default_compatibility"
                    and registry_receiver
                ):
                    kind = "default_compatibility"
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "scoped"
                    and registry_receiver
                ):
                    kind = "scoped"
                    if len(node.args) < 2 and not any(
                        keyword.arg == "scope_context" for keyword in node.keywords
                    ):
                        raise AssertionError(
                            f"scope-free ContractRegistry.scoped at {path}:{node.lineno}"
                        )
                if kind is None:
                    continue
                owner: ast.AST | None = node
                while owner is not None and not isinstance(
                    owner, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    owner = parents.get(owner)
                owner_name = (
                    owner.name
                    if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else "<module>"
                )
                inventory.add(f"{path.relative_to(root).as_posix()}::{owner_name}::{kind}")
    return frozenset(inventory)


def test_contract_registry_production_bindings_are_explicit_and_reviewed() -> None:
    root = Path(__file__).resolve().parents[2]
    assert _contract_registry_binding_inventory(root) == _CONTRACT_REGISTRY_BINDING_INVENTORY


def test_async_persistence_registry_covers_named_non_repository_stores() -> None:
    assert {
        "service/langgraph_gateway/enforcement_store.py",
        "integrations/persistence/runs/checkpoint_store.py",
        "integrations/persistence/runs/token_snapshot_store.py",
    } <= ASYNC_PERSISTENCE_MODULES


def test_persistence_module_discovery_is_fully_classified() -> None:
    discovered = _discover_persistence_shaped_modules(_SOURCE_ROOT)

    assert discovered - ASYNC_PERSISTENCE_MODULES - ASYNC_NON_PERSISTENCE_MODULES == set()
    assert discovered >= ASYNC_NON_PERSISTENCE_MODULES
    assert _raw_async_repository_violations(_SOURCE_ROOT, ASYNC_NON_PERSISTENCE_MODULES) == set()


def test_new_persistence_shaped_module_requires_explicit_classification(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "service/webhooks"
    directory.mkdir(parents=True)
    (directory / "new_repository.py").write_text(
        "async def unsafe(raw):\n    await raw.execute('DELETE FROM rows')\n"
    )

    discovered = _discover_persistence_shaped_modules(tmp_path)

    assert discovered - ASYNC_PERSISTENCE_MODULES - ASYNC_NON_PERSISTENCE_MODULES == {
        "service/webhooks/new_repository.py"
    }


def test_raw_async_repository_guard_reports_new_transaction_and_execute_calls(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "unsafe_repository.py"
    repository.write_text(
        "async def unsafe(database):\n"
        "    async with database.transaction() as connection:\n"
        "        await connection.execute('DELETE FROM runs')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"unsafe_repository.py"}))

    assert any("::transaction#" in violation for violation in violations)
    assert any("::execute#" in violation for violation in violations)


def test_raw_async_repository_guard_tracks_aliases_and_getattr(tmp_path: Path) -> None:
    repository = tmp_path / "unsafe_repository.py"
    repository.write_text(
        "async def unsafe(database):\n"
        "    open_transaction = database.transaction\n"
        "    async with open_transaction() as connection:\n"
        "        run_statement = getattr(connection, 'execute')\n"
        "        await run_statement('DELETE FROM runs')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"unsafe_repository.py"}))

    assert any("::transaction#" in violation for violation in violations)
    assert any("::execute#" in violation for violation in violations)


def test_registered_non_repository_store_is_scanned(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def unsafe(database):\n"
        "    async with database.transaction() as connection:\n"
        "        await connection.execute('DELETE FROM runs')\n"
        "        return await connection.fetch_one('SELECT 1')\n"
    )
    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert any("registered_store.py::unsafe::transaction#" in item for item in violations)
    assert any("registered_store.py::unsafe::execute#" in item for item in violations)
    assert any("registered_store.py::unsafe::fetch_one#" in item for item in violations)


def test_registered_store_ignores_non_database_execute_receivers(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(database, pipe):\n"
        "    await pipe.execute()\n"
        "    async with database.transaction() as connection:\n"
        "        await connection.execute('DELETE FROM runs')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert sum("::execute#" in item for item in violations) == 1


def test_raw_async_guard_propagates_connection_receiver_provenance(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(database, holder):\n"
        "    async with database.transaction() as connection:\n"
        "        alias = connection\n"
        "        await alias.execute('DELETE FROM runs')\n"
        "        read = getattr(alias, 'fetch_one')\n"
        "        await read('SELECT 1')\n"
        "        _, tuple_alias = (object(), alias)\n"
        "        holder.connection = tuple_alias\n"
        "        return await holder.connection.fetch_all('SELECT 1')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert any("::execute#" in item for item in violations)
    assert any("::fetch_one#" in item for item in violations)
    assert any("::fetch_all#" in item for item in violations)


def test_raw_async_guard_seeds_annotated_connection_parameters(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "from zeroth.platform.storage import AsyncConnection\n"
        "async def persist(raw: AsyncConnection):\n"
        "    alias: AsyncConnection = raw\n"
        "    await alias.execute('DELETE FROM runs')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert any("::execute#" in item for item in violations)


def test_raw_async_guard_seeds_direct_connection_acquisition(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(pool):\n"
        "    raw = await pool.acquire()\n"
        "    alias = raw\n"
        "    return await alias.fetch_one('SELECT 1')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert any("::fetch_one#" in item for item in violations)


def test_raw_async_guard_keeps_untainted_clients_and_pipelines_clean(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(pipe, client):\n"
        "    await pipe.execute()\n"
        "    await client.fetch_one('key')\n"
        "    read = getattr(client, 'fetch_all')\n"
        "    return await read('key')\n"
    )

    assert _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"})) == set()


def test_raw_async_guard_ignores_non_database_unit_of_work_transaction(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(unit_of_work):\n"
        "    async with unit_of_work.transaction() as event_batch:\n"
        "        await event_batch.publish()\n"
    )

    assert _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"})) == set()


def test_raw_async_guard_rejects_unannotated_raw_connection_receiver(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(raw):\n"
        "    await raw.execute('DELETE FROM runs')\n"
        "    return await getattr(raw, 'fetch_one')('SELECT 1')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert any("::execute#" in item for item in violations)
    assert any("::fetch_one#" in item for item in violations)


def test_missing_registered_persistence_module_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="missing_store.py"):
        _raw_async_repository_violations(tmp_path, frozenset({"missing_store.py"}))
