from __future__ import annotations

import ast
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
)
from zeroth.platform.storage.scoped_table import (
    ASYNC_PERSISTENCE_MODULES,
    ECON_MIGRATION_SCOPE_DEFINITIONS,
    SERVICE_SCOPE_DEFINITIONS,
)

_ECON_PLANE_ROOT = Path(econ_plane_paths[0])
_SOURCE_ROOT = _ECON_PLANE_ROOT.parents[1]
_GLOBAL_TABLES = {"pricing_catalog", "tool_pricing_catalog", "roles", "user_roles"}


@pytest.fixture(scope="module")
def migration_head_tables(tmp_path_factory: pytest.TempPathFactory) -> tuple[set[str], set[str]]:
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

    def table_names(url: str) -> set[str]:
        engine = create_engine(url)
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    return table_names(service_url), table_names(econ_url)


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
    migration_head_tables: tuple[set[str], set[str]],
) -> None:
    service_tables, _ = migration_head_tables
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
    migration_head_tables: tuple[set[str], set[str]],
) -> None:
    _, econ_tables = migration_head_tables
    mapper_definitions = [model.scope_definition for model in _econ_mapper_classes()]
    registry = ResourceScopeRegistry([*mapper_definitions, *ECON_MIGRATION_SCOPE_DEFINITIONS])

    assert econ_tables <= {definition.table_name for definition in registry.definitions}
    for table_name in econ_tables - {
        "alembic_version",
        "_zeroth_20260811_04_auth_scope",
    }:
        assert registry.definition_for_table(table_name) is next(
            definition for definition in mapper_definitions if definition.table_name == table_name
        )
    assert {definition.table_name for definition in ECON_MIGRATION_SCOPE_DEFINITIONS} == {
        "alembic_version",
        "_zeroth_20260811_04_auth_scope",
    }


def test_service_workspace_scope_definitions_match_head_columns(
    migration_head_tables: tuple[set[str], set[str]],
) -> None:
    del migration_head_tables  # The fixture proves this assertion is against the live head.
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


# Existing service signatures remain route-bound to raw Session until ZER-44 Task 5.
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
    "connectors/service.py": {
        "_attempt_send",
        "_enabled_connectors",
        "_record_delivery",
        "configure_connector",
        "connector_status",
        "enqueue_connector_event",
        "get_or_create_connector_config",
        "list_connector_configs",
        "list_outbox",
        "outbox_counts",
        "process_outbox_batch",
        "render_prometheus_metrics",
        "retry_outbox_item",
        "set_connector_enabled",
    },
    "costing/service.py": {
        "_lookup_pricing",
        "create_cost_profile",
        "create_pricing_catalog",
        "estimate_cost_for_period",
        "get_cost_profile",
        "latest_cost_estimate",
    },
    "counterfactual/service.py": {"estimate_history", "latest_estimate", "run_evaluation"},
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
    "instrumentation/service.py": {"ingest_execution", "ingest_outcome", "query_outcomes"},
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
    _CONNECTION_NAMES = {"conn", "connection", "transaction"}

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.function = "<module>"
        self.violations: list[str] = []
        self._counts: dict[tuple[str, str], int] = {}

    def _record(self, method: str) -> None:
        key = (self.function, method)
        ordinal = self._counts.get(key, 0) + 1
        self._counts[key] = ordinal
        self.violations.append(f"{self.relative_path}::{self.function}::{method}#{ordinal}")

    @classmethod
    def _is_connection_receiver(cls, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in cls._CONNECTION_NAMES
        if isinstance(node, ast.Attribute):
            return node.attr == "connection" or cls._is_connection_receiver(node.value)
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = previous

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in self._RAW_METHODS
            and (node.args[1].value == "transaction" or self._is_connection_receiver(node.args[0]))
        ):
            self._record(str(node.args[1].value))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "transaction" or (
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
contracts/registry/registry.py::_fetch_row::fetch_one#1
contracts/registry/registry.py::_fetch_row::transaction#1
contracts/registry/registry.py::_now::fetch_one#1
contracts/registry/registry.py::_now::transaction#1
contracts/registry/registry.py::delete::execute#1
contracts/registry/registry.py::delete::execute#2
contracts/registry/registry.py::delete::transaction#1
contracts/registry/registry.py::latest_version::fetch_one#1
contracts/registry/registry.py::latest_version::transaction#1
contracts/registry/registry.py::list_names::fetch_all#1
contracts/registry/registry.py::list_names::transaction#1
contracts/registry/registry.py::list_versions::fetch_all#1
contracts/registry/registry.py::list_versions::transaction#1
contracts/registry/registry.py::register::execute#1
contracts/registry/registry.py::register::fetch_one#1
contracts/registry/registry.py::register::transaction#1
contracts/registry/registry.py::register_schema::execute#1
contracts/registry/registry.py::register_schema::fetch_one#1
contracts/registry/registry.py::register_schema::transaction#1
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


def test_async_persistence_registry_covers_named_non_repository_stores() -> None:
    assert {
        "service/langgraph_gateway/enforcement_store.py",
        "integrations/persistence/runs/checkpoint_store.py",
        "integrations/persistence/runs/token_snapshot_store.py",
    } <= ASYNC_PERSISTENCE_MODULES


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


def test_registered_non_repository_store_is_scanned_but_unregistered_file_is_not(
    tmp_path: Path,
) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def unsafe(database):\n"
        "    async with database.transaction() as connection:\n"
        "        await connection.execute('DELETE FROM runs')\n"
        "        return await connection.fetch_one('SELECT 1')\n"
    )
    (tmp_path / "unrelated_repository.py").write_text(
        "async def unrelated(database):\n"
        "    async with database.transaction() as connection:\n"
        "        await connection.execute('DELETE FROM unrelated')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert any("registered_store.py::unsafe::transaction#" in item for item in violations)
    assert any("registered_store.py::unsafe::execute#" in item for item in violations)
    assert any("registered_store.py::unsafe::fetch_one#" in item for item in violations)
    assert all("unrelated_repository.py" not in item for item in violations)


def test_registered_store_ignores_non_database_execute_receivers(tmp_path: Path) -> None:
    (tmp_path / "registered_store.py").write_text(
        "async def persist(database, pipe):\n"
        "    await pipe.execute()\n"
        "    async with database.transaction() as connection:\n"
        "        await connection.execute('DELETE FROM runs')\n"
    )

    violations = _raw_async_repository_violations(tmp_path, frozenset({"registered_store.py"}))

    assert sum("::execute#" in item for item in violations) == 1


def test_missing_registered_persistence_module_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="missing_store.py"):
        _raw_async_repository_violations(tmp_path, frozenset({"missing_store.py"}))
