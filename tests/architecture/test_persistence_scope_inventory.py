from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest
from sqlalchemy import inspect

from zeroth.econ.plane import __path__ as econ_plane_paths
from zeroth.econ.plane.database import Base
from zeroth.platform.storage.scoping import (
    ResourceOperation,
    ResourceScope,
    ResourceScopeDefinition,
    ResourceScopeRegistry,
)

_ECON_PLANE_ROOT = Path(econ_plane_paths[0])
_GLOBAL_TABLES = {"pricing_catalog", "tool_pricing_catalog", "roles", "user_roles"}


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


class _RawSessionVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.function = "<module>"
        self.violations: list[str] = []
        self._counts: dict[tuple[str, str], int] = {}
        self._session_names = {"Session"}
        self._sessionmaker_names = {"sessionmaker"}
        self._factory_names = {"SessionLocal"}
        self._orm_module_aliases = {"sqlalchemy.orm"}
        self._database_module_aliases = {"zeroth.econ.plane.database"}
        self._tainted_result_names: set[str] = set()

    def _record(self, kind: str, detail: str) -> None:
        key = (kind, f"{self.function}:{detail}")
        ordinal = self._counts.get(key, 0) + 1
        self._counts[key] = ordinal
        self.violations.append(f"{self.relative_path}::{kind}::{key[1]}#{ordinal}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.function
        previous_taint = self._tainted_result_names
        self.function = node.name
        self._tainted_result_names = set()
        self.generic_visit(node)
        self.function = previous
        self._tainted_result_names = previous_taint

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.visit_FunctionDef(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
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
        dotted = self._dotted_name(node.func)
        if dotted and dotted.rsplit(".", 1)[-1] in {"execute", "scalars"}:
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
        if self._is_sessionmaker_call(node.value):
            for target in node.targets:
                self._factory_names.update(self._assigned_names(target))
        self._update_result_taint(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self._is_sessionmaker_call(node.value):
            self._factory_names.update(self._assigned_names(node.target))
        if node.value is not None:
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
        if dotted in self._session_names or any(
            dotted == f"{module}.Session" for module in self._orm_module_aliases
        ):
            self._record("construction", dotted or "Session")
        if dotted in self._factory_names:
            self._record("construction", dotted)
        if any(dotted == f"{module}.SessionLocal" for module in self._database_module_aliases):
            self._record("construction", dotted or "SessionLocal")
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
        dotted = self._dotted_name(node)
        root_name = dotted.split(".", 1)[0] if dotted else ""
        raw_session_like = root_name in {"db", "raw_session", "session"}
        tainted_escape = node.attr in {
            "connection",
            "context",
            "raw",
            "root_connection",
        } and self._is_tainted_result(node.value)
        if tainted_escape or (
            node.attr in {"bind", "connection", "get_bind", "query"} and raw_session_like
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
    "auth/service.py": {"issue_token"},
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
        "add_ground_truth_rows",
        "compute_calibration_summary",
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
