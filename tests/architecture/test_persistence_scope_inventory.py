from __future__ import annotations

import ast
import fnmatch
import importlib
import inspect as python_inspect
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
from zeroth.governance.audit import AuditRepository

_ECON_PLANE_ROOT = Path(econ_plane_paths[0])
_SOURCE_ROOT = _ECON_PLANE_ROOT.parents[1]
_GLOBAL_TABLES = {"pricing_catalog", "tool_pricing_catalog", "roles", "user_roles"}


_AUDIT_REPOSITORY_CLASS = ("zeroth", "governance", "audit", "AuditRepository")
_AUDIT_REPOSITORY_MODULE = _AUDIT_REPOSITORY_CLASS[:-1]
_AUDIT_REPOSITORY_IMPLEMENTATION_MODULE = (*_AUDIT_REPOSITORY_MODULE, "repository")
_AUDIT_REPOSITORY_OPERATION_NAMES = frozenset(
    {
        "configure_capture",
        "crypto_erase",
        "crypto_erase_in_transaction",
        "get",
        "list",
        "list_by_deployment",
        "list_by_graph_version",
        "list_by_node",
        "list_by_run",
        "list_by_run_in_transaction",
        "list_by_thread",
        "list_erasable",
        "list_erasable_in_transaction",
        "write",
        "write_many",
    }
)
_AUDIT_REPOSITORY_TYPED_COLLABORATORS = {
    ("zeroth.governance.audit.delivery_state", "AuditRecordWriter"): frozenset({"write"}),
}
_AuditRepositoryCallSite = tuple[str, tuple[str, ...] | None, int, int]
_AUDIT_REPOSITORY_REVIEWED_COLLABORATOR_EDGES = {
    "examples/04_native_tool.py": {
        (("main",), ("demo", "service", "audit_repository")): _AUDIT_REPOSITORY_OPERATION_NAMES,
    },
    "examples/21_policy_block.py": {
        (("main",), ("demo", "service", "audit_repository")): _AUDIT_REPOSITORY_OPERATION_NAMES,
    },
    "examples/24_audit_query.py": {
        (("main",), ("demo", "service", "audit_repository")): _AUDIT_REPOSITORY_OPERATION_NAMES,
    },
    "src/zeroth/governance/audit/verifier.py": {
        (("AuditContinuityVerifier", "verify_deployment"), ("self", "_repository")): frozenset(
            {"list_by_deployment"}
        ),
        (("AuditContinuityVerifier", "verify_run"), ("self", "_repository")): frozenset(
            {"list_by_run"}
        ),
    },
    "src/zeroth/governance/audit/delivery_worker.py": {
        (("DeliveryWorker", "_attempt"), ("self", "_writer")): frozenset({"write"}),
    },
    "src/zeroth/governance/approvals/service.py": {
        (("ApprovalService", "_record_api_audit"), ("self", "audit_repository")): frozenset(
            {"write"}
        ),
        (("ApprovalService", "_record_decision_audit"), ("self", "audit_repository")): frozenset(
            {"write"}
        ),
    },
    "src/zeroth/governance/retention/erasure_service.py": {
        (("RetentionErasureService", "erase_run"), ("self", "_audits")): frozenset(
            {"crypto_erase_in_transaction", "list_by_run", "list_by_run_in_transaction"}
        ),
        (("RetentionErasureService", "purge_audits"), ("self", "_audits")): frozenset(
            {"crypto_erase_in_transaction", "list_erasable_in_transaction"}
        ),
    },
    "src/zeroth/runtime/orchestration/audit_recorder.py": {
        **{
            (("RuntimeAuditRecorder", owner), ("self", "audit_repository")): frozenset({"write"})
            for owner in {
                "record_failed_branch_execution",
                "record_failed_execution",
                "record_history",
                "record_policy_rejection",
            }
        },
    },
    "src/zeroth/runtime/orchestration/parallel_executor.py": {
        (
            ("RuntimeParallelExecutor", "execute_fan_out", "branch_coro_factory"),
            ("self", "audit_recorder", "audit_repository"),
        ): frozenset({"write"}),
    },
    "src/zeroth/runtime/orchestration/run_worker.py": {
        (("RunWorker", "_record_worker_audit"), ("audit_repository",)): frozenset({"write"}),
    },
    "src/zeroth/service/api/audit_api.py": {
        (
            ("register_audit_routes", "_verify_run_chain"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list_by_run"}),
        (
            ("register_audit_routes", "get_deployment_evidence"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list_by_deployment"}),
        (
            ("register_audit_routes", "get_deployment_timeline"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list_by_deployment"}),
        (
            ("register_audit_routes", "get_run_evidence"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list_by_run"}),
        (
            ("register_audit_routes", "get_run_timeline"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list_by_run"}),
        (("register_audit_routes", "list_audits"), ("bootstrap", "audit_repository")): frozenset(
            {"list"}
        ),
    },
    "src/zeroth/service/api/econ_analytics_api.py": {
        (("_windowed_runs_and_audits",), ("bootstrap", "audit_repository")): frozenset({"list"}),
    },
    "src/zeroth/service/api/retention_api.py": {
        (
            ("register_retention_routes", "_erase_tenant"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list_erasable"}),
        (
            ("register_retention_routes", "_require_run_tenant"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list"}),
    },
    "src/zeroth/service/api/rightsizing_api.py": {
        (
            ("register_rightsizing_routes", "rightsizing_opportunities"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list"}),
        (
            ("register_rightsizing_routes", "run_rightsizing_experiment"),
            ("bootstrap", "audit_repository"),
        ): frozenset({"list"}),
    },
}


def _dotted_ast_path(node: ast.AST) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.NamedExpr):
        return _dotted_ast_path(node.target)
    if isinstance(node, ast.Attribute):
        base = _dotted_ast_path(node.value)
        return None if base is None else (*base, node.attr)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        base = _dotted_ast_path(node.args[0])
        return None if base is None else (*base, node.args[1].value)
    return None


def _annotation_type_nodes(node: ast.AST | None) -> tuple[ast.AST, ...]:
    annotation = _parsed_annotation(node)
    if annotation is None:
        return ()
    nodes: list[ast.AST] = []

    def visit(candidate: ast.AST) -> None:
        if isinstance(candidate, ast.BinOp) and isinstance(candidate.op, ast.BitOr):
            visit(candidate.left)
            visit(candidate.right)
            return
        if isinstance(candidate, ast.Subscript):
            base = _dotted_ast_path(candidate.value)
            arguments = (
                candidate.slice.elts
                if isinstance(candidate.slice, ast.Tuple)
                else (candidate.slice,)
            )
            if base is not None and base[-1] == "Annotated":
                if arguments:
                    visit(arguments[0])
                return
            visit(candidate.value)
            for argument in arguments:
                visit(argument)
            return
        if isinstance(candidate, (ast.Tuple, ast.List)):
            for element in candidate.elts:
                visit(element)
            return
        nodes.append(candidate)

    visit(annotation)
    return tuple(nodes)


def _annotation_names(node: ast.AST | None) -> set[str]:
    names: set[str] = set()
    for candidate in _annotation_type_nodes(node):
        dotted = _dotted_ast_path(candidate)
        if dotted is not None:
            names.add(dotted[-1])
    return names


def _receiver_annotation_type_nodes(node: ast.AST | None) -> tuple[ast.AST, ...]:
    annotation = _parsed_annotation(node)
    if annotation is None:
        return ()
    nodes: list[ast.AST] = []

    def visit(candidate: ast.AST) -> None:
        if isinstance(candidate, ast.BinOp) and isinstance(candidate.op, ast.BitOr):
            visit(candidate.left)
            visit(candidate.right)
            return
        if isinstance(candidate, ast.Subscript):
            base = _dotted_ast_path(candidate.value)
            if base is not None and base[-1] in {"Annotated", "Optional", "Union"}:
                arguments = (
                    candidate.slice.elts
                    if isinstance(candidate.slice, ast.Tuple)
                    else (candidate.slice,)
                )
                if base[-1] == "Annotated":
                    arguments = arguments[:1]
                for argument in arguments:
                    visit(argument)
                return
        nodes.append(candidate)

    visit(annotation)
    return tuple(nodes)


def _is_potential_repository_annotation(node: ast.AST | None) -> bool:
    return any(
        (path := _dotted_ast_path(candidate)) is not None and path[-1] == "AuditRepository"
        for candidate in _annotation_type_nodes(node)
    )


def _ast_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    owner: ast.AST | None = node
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = parents.get(owner)
    return owner if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


_PROVENANCE_SCOPE_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _enclosing_provenance_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    owner: ast.AST | None = node
    while owner is not None:
        if isinstance(owner, _PROVENANCE_SCOPE_NODES):
            if isinstance(owner, ast.Lambda):
                defaults = {
                    *owner.args.defaults,
                    *(default for default in owner.args.kw_defaults if default is not None),
                }
                ancestor: ast.AST | None = node
                while ancestor is not None and ancestor is not owner:
                    if ancestor in defaults:
                        owner = parents.get(owner)
                        break
                    ancestor = parents.get(ancestor)
                else:
                    return owner
                continue
            if isinstance(owner, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                first_iter = owner.generators[0].iter
                ancestor: ast.AST | None = node
                while ancestor is not None and ancestor is not owner:
                    if ancestor is first_iter:
                        owner = parents.get(owner)
                        break
                    ancestor = parents.get(ancestor)
                else:
                    return owner
                continue
            return owner
        owner = parents.get(owner)
    return None


def _enclosing_class(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    owner: ast.AST | None = node
    while owner is not None and not isinstance(owner, ast.ClassDef):
        owner = parents.get(owner)
    return owner if isinstance(owner, ast.ClassDef) else None


def _lexical_owner_path(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> tuple[str, ...]:
    names: list[str] = []
    owner: ast.AST | None = node
    while owner is not None:
        if isinstance(owner, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(owner.name)
        owner = parents.get(owner)
    return tuple(reversed(names))


def _function_argument_names(
    owner: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | None,
) -> set[str]:
    if owner is None:
        return set()
    names = {
        argument.arg
        for argument in (
            *owner.args.posonlyargs,
            *owner.args.args,
            *owner.args.kwonlyargs,
        )
    }
    names.update(
        argument.arg for argument in (owner.args.vararg, owner.args.kwarg) if argument is not None
    )
    return names


def _is_lambda_body_binding(
    node: ast.AST,
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    ancestor = parents.get(node)
    while ancestor is not None and ancestor is not owner:
        if isinstance(ancestor, ast.Lambda):
            defaults = {
                *ancestor.args.defaults,
                *(default for default in ancestor.args.kw_defaults if default is not None),
            }
            candidate: ast.AST | None = node
            found_in_default = False
            while candidate is not None and candidate is not ancestor:
                if candidate in defaults:
                    found_in_default = True
                    break
                candidate = parents.get(candidate)
            if found_in_default:
                ancestor = parents.get(ancestor)
                continue
            return True
        ancestor = parents.get(ancestor)
    return False


def _parsed_annotation(node: ast.AST | None) -> ast.AST | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    return node


def _provenance_scope(
    node: ast.AST,
    path: tuple[str, ...] | None,
    parents: dict[ast.AST, ast.AST],
) -> ast.AST | None:
    return _enclosing_provenance_scope(node, parents)


def _provenance_scope_chain(
    scope: ast.AST | None, parents: dict[ast.AST, ast.AST]
) -> tuple[ast.AST | None, ...]:
    chain: list[ast.AST | None] = [scope]
    while chain[-1] is not None:
        chain.append(_enclosing_provenance_scope(parents.get(chain[-1], chain[-1]), parents))
    return tuple(chain)


def _is_expression_shadowed(
    node: ast.AST,
    receiver: tuple[str, ...] | None,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if receiver is None:
        return False
    owner: ast.AST | None = node
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(owner, ast.Lambda) and receiver[0] in _function_argument_names(owner):
            defaults = {
                *owner.args.defaults,
                *(default for default in owner.args.kw_defaults if default is not None),
            }
            ancestor: ast.AST | None = node
            while ancestor is not None and ancestor is not owner:
                if ancestor in defaults:
                    break
                ancestor = parents.get(ancestor)
            else:
                return True
        if isinstance(owner, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for generator in owner.generators:
                if node in ast.walk(generator.iter):
                    break
                if receiver[0] in _bound_names(generator.target):
                    return True
        owner = parents.get(owner)
    return False


def _bound_repository_operations(
    bound_paths: dict[tuple[ast.AST | None, tuple[str, ...]], frozenset[str]],
    scope: ast.AST | None,
    receiver: tuple[str, ...] | None,
    parents: dict[ast.AST, ast.AST],
    local_names: dict[ast.AST, set[str]] | None = None,
    competing_names: dict[ast.AST, set[str]] | None = None,
    invalidated_paths: set[tuple[ast.AST | None, tuple[str, ...]]] | None = None,
) -> frozenset[str] | None:
    if receiver is None:
        return None
    for candidate_scope in _provenance_scope_chain(scope, parents):
        if isinstance(candidate_scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            receiver[0] in declaration.names
            for declaration in candidate_scope.body
            if isinstance(declaration, ast.Global)
        ):
            if invalidated_paths is not None and (None, receiver) in invalidated_paths:
                return None
            return bound_paths.get((None, receiver))
        if invalidated_paths is not None and (candidate_scope, receiver) in invalidated_paths:
            return None
        if (
            competing_names is not None
            and candidate_scope is not None
            and receiver[0] in competing_names.get(candidate_scope, set())
        ):
            return None
        operations = bound_paths.get((candidate_scope, receiver))
        if operations is not None:
            return operations
        if (
            local_names is not None
            and candidate_scope is not None
            and receiver[0] in local_names.get(candidate_scope, set())
        ):
            if receiver[0] == "self" and isinstance(parents.get(candidate_scope), ast.ClassDef):
                return bound_paths.get((parents[candidate_scope], receiver))
            return None
    if scope is not None and receiver[0] == "self":
        class_scope = _enclosing_class(scope, parents)
        if class_scope is not None:
            return bound_paths.get((class_scope, receiver))
    return None


def _visible_repository_names(
    scope: ast.AST | None,
    module_repository_names: set[str],
    local_repository_names: dict[ast.AST, set[str]],
    local_names: dict[ast.AST, set[str]],
    parents: dict[ast.AST, ast.AST],
) -> set[str]:
    candidates = set(module_repository_names)
    for names in local_repository_names.values():
        candidates.update(names)
    visible: set[str] = set()
    for name in candidates:
        for candidate_scope in _provenance_scope_chain(scope, parents):
            if candidate_scope is None:
                if name in module_repository_names:
                    visible.add(name)
                break
            if name in local_repository_names.get(candidate_scope, set()):
                visible.add(name)
                break
            if name in local_names.get(candidate_scope, set()):
                break
    return visible


def _resolved_scoped_factory(
    node: ast.AST,
    scope: ast.FunctionDef | ast.AsyncFunctionDef | None,
    repository_names: set[str],
    module_names: dict[str, tuple[str, ...]],
    local_alias_events: dict[
        ast.AST, dict[str, list[tuple[tuple[int, int], tuple[str, ...] | None]]]
    ],
    local_names: dict[ast.AST, set[str]],
    parents: dict[ast.AST, ast.AST],
) -> tuple[tuple[str, ...] | None, bool]:
    dotted = _dotted_ast_path(node)
    if dotted is not None:
        module_identity = _resolved_audit_repository_name(node, repository_names, module_names)
        for candidate_scope in _provenance_scope_chain(scope, parents):
            if candidate_scope is None:
                return module_identity, False
            events = local_alias_events.get(candidate_scope, {}).get(dotted[0], [])
            visible_events = [
                identity
                for position, identity in events
                if position < (node.lineno, node.col_offset)
            ]
            if visible_events:
                identity = visible_events[-1]
                resolved_identity = (
                    None
                    if identity is None
                    else _canonical_audit_repository_name((*identity, *dotted[1:]))
                )
                was_scoped_factory = any(
                    identity is not None
                    and _canonical_audit_repository_name((*identity, *dotted[1:]))
                    == (*_AUDIT_REPOSITORY_CLASS, "scoped")
                    for identity in visible_events
                )
                return (
                    resolved_identity,
                    resolved_identity is None and was_scoped_factory,
                )
            if dotted[0] in local_names.get(candidate_scope, set()):
                return None, module_identity == (*_AUDIT_REPOSITORY_CLASS, "scoped")
        return module_identity, False
    return _resolved_audit_repository_name(node, repository_names, module_names), False


_AliasIdentity = tuple[str, ...] | None
_AliasEvents = dict[str, list[tuple[tuple[int, int], _AliasIdentity]]]


def _must_alias_events(
    owner: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
    repository_names: set[str],
    module_names: dict[str, tuple[str, ...]],
    *,
    initial_state: dict[str, _AliasIdentity] | None = None,
    potential_import_aliases: bool = False,
) -> _AliasEvents:
    events: _AliasEvents = {}

    def resolve(value: ast.AST, state: dict[str, _AliasIdentity]) -> _AliasIdentity:
        if isinstance(value, ast.IfExp):
            identities = (resolve(value.body, state), resolve(value.orelse, state))
            return identities[0] if identities[0] == identities[1] else None
        dotted = _dotted_ast_path(value)
        if dotted is not None and dotted[0] in state:
            identity = state[dotted[0]]
            return (
                None
                if identity is None
                else _canonical_audit_repository_name((*identity, *dotted[1:]))
            )
        return _resolved_audit_repository_name(value, repository_names, module_names)

    def leaf_identities(
        value: ast.AST, state: dict[str, _AliasIdentity]
    ) -> tuple[_AliasIdentity, ...]:
        if isinstance(value, ast.IfExp):
            return (*leaf_identities(value.body, state), *leaf_identities(value.orelse, state))
        return (resolve(value, state),)

    def join(
        left: dict[str, _AliasIdentity], right: dict[str, _AliasIdentity]
    ) -> dict[str, _AliasIdentity]:
        return {
            name: left.get(name) if left.get(name) == right.get(name) else None
            for name in left.keys() | right.keys()
        }

    def record(
        statement: ast.Assign | ast.AnnAssign,
        state: dict[str, _AliasIdentity],
    ) -> None:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target = statement.target
            value = statement.value
        else:
            return
        if isinstance(target, ast.Name):
            identity = resolve(value, state)
            state[target.id] = identity
            if isinstance(value, ast.IfExp) and identity is None:
                for branch_identity in leaf_identities(value, state):
                    if branch_identity is not None:
                        events.setdefault(target.id, []).append(
                            ((statement.lineno, statement.col_offset), branch_identity)
                        )
            events.setdefault(target.id, []).append(
                ((statement.lineno, statement.col_offset), identity)
            )

    def merge_event(statement: ast.stmt, state: dict[str, _AliasIdentity]) -> None:
        position = (statement.end_lineno, statement.end_col_offset)
        for name, identity in state.items():
            events.setdefault(name, []).append((position, identity))

    def invalidate(statement: ast.stmt, names: set[str], state: dict[str, _AliasIdentity]) -> None:
        for name in names:
            state[name] = None
        merge_event(statement, state)

    def statement_named_expression_names(statement: ast.stmt) -> set[str]:
        names: set[str] = set()

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.NamedExpr):
                names.update(_bound_names(node.target))
                return
            if isinstance(node, ast.Lambda):
                for default in (*node.args.defaults, *node.args.kw_defaults):
                    if default is not None:
                        visit(default)
                return
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, ast.stmt):
                    visit(child)

        visit(statement)
        return names

    def walk_block(
        statements: list[ast.stmt], state: dict[str, _AliasIdentity]
    ) -> dict[str, _AliasIdentity]:
        state = dict(state)
        for statement in statements:
            expression_names = statement_named_expression_names(statement)
            if expression_names:
                invalidate(statement, expression_names, state)
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                record(statement, state)
            elif isinstance(statement, ast.If):
                state = join(
                    walk_block(statement.body, state),
                    walk_block(statement.orelse, state),
                )
                merge_event(statement, state)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                loop_state = dict(state)
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    invalidate(statement, _bound_names(statement.target), loop_state)
                body_state = walk_block(statement.body, loop_state)
                else_state = walk_block(statement.orelse, join(state, body_state))
                if any(
                    isinstance(node, ast.Break)
                    for body_statement in statement.body
                    for node in ast.walk(body_statement)
                ):
                    state = join(body_state, else_state)
                else:
                    state = else_state
                merge_event(statement, state)
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                body_state = walk_block(statement.body, state)
                path_states = [walk_block(statement.orelse, body_state)]
                for handler in statement.handlers:
                    handler_state = dict(state)
                    if handler.name:
                        handler_state[handler.name] = None
                        events.setdefault(handler.name, []).append(
                            ((handler.lineno, handler.col_offset), None)
                        )
                    path_states.append(walk_block(handler.body, handler_state))
                state = path_states[0]
                for path_state in path_states[1:]:
                    state = join(state, path_state)
                state = walk_block(statement.finalbody, state)
                merge_event(statement, state)
            elif isinstance(statement, ast.Match):
                path_states = []
                for case in statement.cases:
                    case_state = dict(state)
                    invalidate(
                        statement,
                        _bound_names(case.pattern),
                        case_state,
                    )
                    path_states.append(walk_block(case.body, case_state))
                if not any(
                    isinstance(case.pattern, ast.MatchAs)
                    and case.pattern.pattern is None
                    and case.pattern.name is None
                    and case.guard is None
                    for case in statement.cases
                ):
                    path_states.append(state)
                state = path_states[0]
                for path_state in path_states[1:]:
                    state = join(state, path_state)
                merge_event(statement, state)
            elif isinstance(statement, ast.ImportFrom) and potential_import_aliases:
                for alias in statement.names:
                    name = alias.asname or alias.name.split(".")[0]
                    state[name] = (
                        _AUDIT_REPOSITORY_CLASS
                        if (
                            alias.name == "AuditRepository"
                            and statement.module
                            not in {
                                "zeroth.governance.audit",
                                "zeroth.governance.audit.repository",
                            }
                        )
                        else None
                    )
                merge_event(statement, state)
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                invalidate(
                    statement,
                    {alias.asname or alias.name.split(".")[0] for alias in statement.names},
                    state,
                )
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                invalidate(statement, {statement.name}, state)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                body_state = dict(state)
                invalidate(
                    statement,
                    set().union(
                        *(
                            _bound_names(item.optional_vars)
                            for item in statement.items
                            if item.optional_vars is not None
                        )
                    ),
                    body_state,
                )
                state = join(state, walk_block(statement.body, body_state))
                merge_event(statement, state)
        return state

    walk_block(owner.body, initial_state or {})
    return events


def _visible_annotation_repository_names(
    node: ast.AST,
    scope: ast.AST | None,
    module_repository_names: set[str],
    local_repository_names: dict[ast.AST, set[str]],
    local_names: dict[ast.AST, set[str]],
    local_alias_events: dict[ast.AST, _AliasEvents],
    parents: dict[ast.AST, ast.AST],
) -> set[str]:
    visible = _visible_repository_names(
        scope,
        module_repository_names,
        local_repository_names,
        local_names,
        parents,
    )
    position = (node.lineno, node.col_offset)
    shadowed_names: set[str] = set()
    for candidate_scope in _provenance_scope_chain(scope, parents):
        events_by_name = local_alias_events.get(candidate_scope, {})
        candidate_names = local_names.get(candidate_scope, set()) | set(events_by_name)
        for name in candidate_names - shadowed_names:
            visible.discard(name)
            visible_events = [
                (event, identity)
                for event, identity in events_by_name.get(name, [])
                if event < position
            ]
            last_event = visible_events[-1][0] if visible_events else None
            if any(
                event == last_event and identity == _AUDIT_REPOSITORY_CLASS
                for event, identity in visible_events
            ):
                visible.add(name)
            shadowed_names.add(name)
    return visible


def _visible_potential_annotation_repository_names(
    node: ast.AST,
    scope: ast.AST | None,
    potential_repository_names: set[str],
    local_names: dict[ast.AST, set[str]],
    potential_alias_events: dict[ast.AST | None, _AliasEvents],
    parents: dict[ast.AST, ast.AST],
) -> set[str]:
    visible = set(potential_repository_names)
    position = (node.lineno, node.col_offset)
    shadowed_names: set[str] = set()
    for candidate_scope in _provenance_scope_chain(scope, parents):
        events_by_name = potential_alias_events.get(candidate_scope, {})
        candidate_names = local_names.get(candidate_scope, set()) | set(events_by_name)
        for name in candidate_names - shadowed_names:
            visible.discard(name)
            visible_events = [
                identity for event, identity in events_by_name.get(name, []) if event < position
            ]
            if visible_events and visible_events[-1] == _AUDIT_REPOSITORY_CLASS:
                visible.add(name)
            shadowed_names.add(name)
    return visible


def _has_potential_repository_path(
    paths: set[tuple[ast.AST | None, tuple[str, ...]]],
    scope: ast.AST | None,
    receiver: tuple[str, ...] | None,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    return receiver is not None and any(
        (candidate_scope, receiver) in paths
        for candidate_scope in _provenance_scope_chain(scope, parents)
    )


def _bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return set().union(*(_bound_names(element) for element in node.elts))
    if isinstance(node, ast.Starred):
        return _bound_names(node.value)
    if isinstance(node, ast.NamedExpr):
        return _bound_names(node.target)
    if isinstance(node, ast.MatchAs):
        return ({node.name} if node.name is not None else set()) | (
            _bound_names(node.pattern) if node.pattern is not None else set()
        )
    if isinstance(node, ast.MatchStar):
        return {node.name} if node.name is not None else set()
    if isinstance(node, ast.MatchMapping):
        names = set().union(*(_bound_names(pattern) for pattern in node.patterns))
        if node.rest is not None:
            names.add(node.rest)
        return names
    if isinstance(node, ast.MatchSequence):
        return set().union(*(_bound_names(pattern) for pattern in node.patterns))
    if isinstance(node, ast.MatchClass):
        return set().union(
            *(_bound_names(pattern) for pattern in (*node.patterns, *node.kwd_patterns))
        )
    if isinstance(node, ast.MatchOr):
        return set().union(*(_bound_names(pattern) for pattern in node.patterns))
    return set()


def _binding_targets(node: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.NamedExpr)):
        return (node.target,)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return tuple(item.optional_vars for item in node.items if item.optional_vars is not None)
    if isinstance(node, ast.Match):
        return tuple(
            target
            for case in node.cases
            for target in (case.pattern, case.guard)
            if target is not None
        )
    return ()


def _assignment_bindings(node: ast.AST) -> tuple[tuple[ast.AST, ast.AST], ...]:
    def structural_pairs(target: ast.AST, value: ast.AST) -> tuple[tuple[ast.AST, ast.AST], ...]:
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            starred = [
                index
                for index, element in enumerate(target.elts)
                if isinstance(element, ast.Starred)
            ]
            if not starred and len(target.elts) == len(value.elts):
                return tuple(
                    pair
                    for target_element, value_element in zip(target.elts, value.elts, strict=True)
                    for pair in structural_pairs(target_element, value_element)
                )
            if len(starred) != 1 or len(value.elts) < len(target.elts) - 1:
                return ((target, value),)
            starred_index = starred[0]
            suffix_length = len(target.elts) - starred_index - 1
            starred_target = target.elts[starred_index]
            assert isinstance(starred_target, ast.Starred)
            pairs: list[tuple[ast.AST, ast.AST]] = []
            for target_element, value_element in zip(
                target.elts[:starred_index], value.elts[:starred_index], strict=True
            ):
                pairs.extend(structural_pairs(target_element, value_element))
            starred_values = value.elts[
                starred_index : len(value.elts) - suffix_length if suffix_length else None
            ]
            pairs.append((starred_target.value, ast.List(elts=starred_values, ctx=ast.Load())))
            if suffix_length:
                for target_element, value_element in zip(
                    target.elts[-suffix_length:], value.elts[-suffix_length:], strict=True
                ):
                    pairs.extend(structural_pairs(target_element, value_element))
            return tuple(pairs)
        return ((target, value),)

    def compositional_pairs(target: ast.AST, value: ast.AST) -> tuple[tuple[ast.AST, ast.AST], ...]:
        if not isinstance(value, ast.IfExp):
            return structural_pairs(target, value)
        body_pairs = compositional_pairs(target, value.body)
        orelse_pairs = compositional_pairs(target, value.orelse)
        if len(body_pairs) > 1 and len(orelse_pairs) == 1 and orelse_pairs[0][0] is target:
            return tuple(
                (
                    body_target,
                    ast.IfExp(test=value.test, body=body_value, orelse=orelse_pairs[0][1]),
                )
                for body_target, body_value in body_pairs
            )
        if len(orelse_pairs) > 1 and len(body_pairs) == 1 and body_pairs[0][0] is target:
            return tuple(
                (
                    orelse_target,
                    ast.IfExp(test=value.test, body=body_pairs[0][1], orelse=orelse_value),
                )
                for orelse_target, orelse_value in orelse_pairs
            )
        if len(body_pairs) != len(orelse_pairs) or any(
            body_target is not orelse_target
            for (body_target, _body_value), (orelse_target, _orelse_value) in zip(
                body_pairs, orelse_pairs, strict=True
            )
        ):
            return ((target, value),)
        return tuple(
            (
                body_target,
                ast.IfExp(test=value.test, body=body_value, orelse=orelse_value),
            )
            for (body_target, body_value), (_orelse_target, orelse_value) in zip(
                body_pairs, orelse_pairs, strict=True
            )
        )

    if isinstance(node, ast.Assign):
        pairs: list[tuple[ast.AST, ast.AST]] = []
        for target in node.targets:
            pairs.extend(compositional_pairs(target, node.value))
        return tuple(pairs)
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return compositional_pairs(node.target, node.value)
    if isinstance(node, ast.NamedExpr):
        return compositional_pairs(node.target, node.value)
    return ()


def _assignment_value_options(value: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(value, ast.IfExp):
        return (*_assignment_value_options(value.body), *_assignment_value_options(value.orelse))
    return (value,)


def _compound_body_shadowed(node: ast.AST, name: str, parents: dict[ast.AST, ast.AST]) -> bool:
    child: ast.AST = node
    owner = parents.get(child)
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if (
            isinstance(owner, (ast.For, ast.AsyncFor))
            and child in owner.body
            and name in _bound_names(owner.target)
        ):
            return True
        if (
            isinstance(owner, (ast.With, ast.AsyncWith))
            and child in owner.body
            and any(
                item.optional_vars is not None and name in _bound_names(item.optional_vars)
                for item in owner.items
            )
        ):
            return True
        child = owner
        owner = parents.get(owner)
    return False


def _function_local_names(
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> set[str]:
    names = _function_argument_names(owner)
    for node in ast.walk(owner):
        if _is_lambda_body_binding(node, owner, parents):
            continue
        if (
            node is not owner
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and _enclosing_function(parents.get(node, node), parents) is owner
        ):
            names.add(node.name)
            continue
        if _enclosing_function(node, parents) is not owner:
            continue
        for target in _binding_targets(node):
            names.update(_bound_names(target))
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    for declaration in owner.body:
        if isinstance(declaration, (ast.Global, ast.Nonlocal)):
            names.difference_update(declaration.names)
    return names


def _provenance_scope_local_names(owner: ast.AST, parents: dict[ast.AST, ast.AST]) -> set[str]:
    if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _function_local_names(owner, parents)
    if isinstance(owner, ast.Lambda):
        return _function_argument_names(owner)
    if isinstance(owner, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return set().union(*(_bound_names(generator.target) for generator in owner.generators))
    return set()


def _function_reassigned_names(
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(owner):
        if _is_lambda_body_binding(node, owner, parents):
            continue
        if (
            node is not owner
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and _enclosing_function(parents.get(node, node), parents) is owner
        ):
            names.add(node.name)
            continue
        if _enclosing_function(node, parents) is not owner:
            continue
        for target in _binding_targets(node):
            names.update(_bound_names(target))
        if isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return names


def _function_competing_binding_names(
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> set[str]:
    counts: dict[str, int] = {}
    for node in ast.walk(owner):
        if _is_lambda_body_binding(node, owner, parents):
            continue
        if (
            node is not owner
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and _enclosing_function(parents.get(node, node), parents) is owner
        ):
            counts[node.name] = counts.get(node.name, 0) + 2
            continue
        if _enclosing_function(node, parents) is not owner:
            continue
        for target in _binding_targets(node):
            for name in _bound_names(target):
                counts[name] = counts.get(name, 0) + 1
    return {name for name, count in counts.items() if count > 1}


def _annotation_repository_operations(
    node: ast.AST | None,
    repository_names: set[str],
    collaborator_names: dict[str, frozenset[str]],
    module_names: dict[str, tuple[str, ...]],
) -> frozenset[str]:
    annotation_types = _receiver_annotation_type_nodes(node)
    if any(
        _dotted_ast_path(candidate) == _AUDIT_REPOSITORY_CLASS
        or _resolved_audit_repository_name(candidate, repository_names, module_names)
        == _AUDIT_REPOSITORY_CLASS
        for candidate in annotation_types
    ):
        return _AUDIT_REPOSITORY_OPERATION_NAMES
    operations: set[str] = set()
    for candidate in annotation_types:
        dotted = _dotted_ast_path(candidate)
        if dotted is not None:
            operations.update(collaborator_names.get(dotted[-1], ()))
    return frozenset(operations)


def _binding_scope(
    node: ast.AST,
    target: tuple[str, ...] | None,
    parents: dict[ast.AST, ast.AST],
) -> ast.AST | None:
    if target is None:
        return _provenance_scope(node, target, parents)
    for scope in _provenance_scope_chain(_provenance_scope(node, target, parents), parents):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return scope
        declarations = [
            declaration
            for declaration in scope.body
            if isinstance(declaration, (ast.Global, ast.Nonlocal))
            and target[0] in declaration.names
        ]
        if not declarations:
            return scope
        if any(isinstance(declaration, ast.Global) for declaration in declarations):
            return None
    return None


def _inside_unmodeled_control_flow(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    owner: ast.AST | None = parents.get(node)
    while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if isinstance(owner, (ast.Try, ast.TryStar, ast.Match)):
            return True
        owner = parents.get(owner)
    return False


def _annotation_class_identity(
    node: ast.AST,
    name: str,
    class_identities: set[tuple[str, ...]],
    parents: dict[ast.AST, ast.AST],
) -> tuple[str, ...] | None:
    owner = _enclosing_function(node, parents)
    owner_path = _lexical_owner_path(owner, parents) if owner is not None else ()
    for index in range(len(owner_path), -1, -1):
        candidate = (*owner_path[:index], name)
        if candidate in class_identities:
            return candidate
    candidate = (name,)
    return candidate if candidate in class_identities else None


def _audit_repository_public_call_provenance(
    root: Path,
) -> tuple[frozenset[_AuditRepositoryCallSite], frozenset[_AuditRepositoryCallSite]]:
    """Return reviewed and potential calls with receiver and source-position identity."""
    reviewed: set[_AuditRepositoryCallSite] = set()
    potential: set[_AuditRepositoryCallSite] = set()
    for search_root in (
        root / "src",
        root / "release",
        root / "apps",
        root / "examples",
        root / "packaging" / "console" / "src",
    ):
        for path in search_root.rglob("*.py"):
            relative_path = path.relative_to(root).as_posix()
            if relative_path == "src/zeroth/governance/audit/repository.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents = _ast_parents(tree)
            local_names = {
                owner: _provenance_scope_local_names(owner, parents)
                for owner in ast.walk(tree)
                if isinstance(owner, _PROVENANCE_SCOPE_NODES)
            }
            competing_names = {
                owner: _function_competing_binding_names(owner, parents)
                for owner in ast.walk(tree)
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            reassigned_paths: set[
                tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, tuple[str, ...]]
            ] = set()
            declared_reassigned_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            for node in ast.walk(tree):
                for target in _binding_targets(node):
                    path_value = _dotted_ast_path(target)
                    if path_value is not None and len(path_value) > 1:
                        reassigned_paths.add((_enclosing_function(node, parents), path_value))
                    elif path_value is not None:
                        owner = _enclosing_function(node, parents)
                        declared_scope = _binding_scope(node, path_value, parents)
                        if declared_scope is not owner:
                            declared_reassigned_paths.add((declared_scope, path_value))
            reassigned_names = {
                owner: _function_reassigned_names(owner, parents)
                for owner in local_names
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            repository_names: set[str] = set()
            potential_local_repository_names: set[str] = set()
            potential_imported_repository_names: set[str] = set()
            collaborator_names: dict[str, frozenset[str]] = {}
            module_names: dict[str, tuple[str, ...]] = {}
            for imported in ast.walk(tree):
                if _enclosing_function(imported, parents) is not None:
                    if isinstance(imported, ast.ImportFrom) and imported.module in {
                        "zeroth.governance.audit",
                        "zeroth.governance.audit.repository",
                    }:
                        potential_local_repository_names.update(
                            alias.asname or alias.name
                            for alias in imported.names
                            if alias.name == "AuditRepository"
                        )
                    elif isinstance(imported, ast.ImportFrom):
                        potential_imported_repository_names.update(
                            alias.asname or alias.name
                            for alias in imported.names
                            if alias.name == "AuditRepository"
                        )
                    continue
                if isinstance(imported, ast.ImportFrom):
                    for alias in imported.names:
                        if (
                            imported.module
                            in {
                                "zeroth.governance.audit",
                                "zeroth.governance.audit.repository",
                            }
                            and alias.name == "AuditRepository"
                        ):
                            repository_names.add(alias.asname or alias.name)
                        elif alias.name == "AuditRepository":
                            potential_imported_repository_names.add(alias.asname or alias.name)
                        collaborator_operations = _AUDIT_REPOSITORY_TYPED_COLLABORATORS.get(
                            (imported.module or "", alias.name)
                        )
                        if collaborator_operations is not None:
                            collaborator_names[alias.asname or alias.name] = collaborator_operations
                elif isinstance(imported, ast.Import):
                    for alias in imported.names:
                        if alias.name in {
                            "zeroth.governance.audit",
                            "zeroth.governance.audit.repository",
                        }:
                            if alias.asname:
                                module_names[alias.asname] = _canonical_audit_repository_name(
                                    tuple(alias.name.split("."))
                                )
                            else:
                                module_names["zeroth"] = ("zeroth",)
            changed = True
            while changed:
                changed = False
                for assigned in ast.walk(tree):
                    if _enclosing_function(assigned, parents) is not None:
                        continue
                    if isinstance(assigned, ast.Assign) and len(assigned.targets) == 1:
                        target = assigned.targets[0]
                        value = assigned.value
                    elif isinstance(assigned, ast.AnnAssign):
                        target = assigned.target
                        value = assigned.value
                    else:
                        continue
                    if not isinstance(target, ast.Name) or value is None:
                        continue
                    binding = _resolved_audit_repository_name(value, repository_names, module_names)
                    if binding == _AUDIT_REPOSITORY_CLASS and target.id not in repository_names:
                        repository_names.add(target.id)
                        changed = True
                    elif binding is not None and target.id not in module_names:
                        module_names[target.id] = binding
                        changed = True
            module_binding_counts: dict[str, int] = {}
            for assigned in ast.walk(tree):
                if _enclosing_function(assigned, parents) is not None:
                    continue
                targets: tuple[ast.AST, ...] = ()
                if isinstance(assigned, ast.Assign):
                    targets = tuple(assigned.targets)
                elif isinstance(assigned, ast.AnnAssign):
                    targets = (assigned.target,)
                for target in targets:
                    for name in _bound_names(target):
                        module_binding_counts[name] = module_binding_counts.get(name, 0) + 1
            ambiguous_module_names = {
                name for name, count in module_binding_counts.items() if count > 1
            }
            potential_module_repository_names = repository_names & ambiguous_module_names
            repository_names.difference_update(ambiguous_module_names)
            for name in ambiguous_module_names:
                module_names.pop(name, None)
            local_repository_names: dict[ast.AST, set[str]] = {}
            changed = True
            while changed:
                changed = False
                for assigned in ast.walk(tree):
                    owner = _enclosing_function(assigned, parents)
                    if owner is None:
                        continue
                    if isinstance(assigned, ast.Assign) and len(assigned.targets) == 1:
                        target = assigned.targets[0]
                        value = assigned.value
                    elif isinstance(assigned, ast.AnnAssign) and assigned.value is not None:
                        target = assigned.target
                        value = assigned.value
                    else:
                        continue
                    if not isinstance(target, ast.Name):
                        continue
                    visible_repository_names = _visible_repository_names(
                        owner,
                        repository_names,
                        local_repository_names,
                        local_names,
                        parents,
                    )
                    if (
                        _resolved_audit_repository_name(
                            value, visible_repository_names, module_names
                        )
                        == _AUDIT_REPOSITORY_CLASS
                        and target.id not in local_repository_names.setdefault(owner, set())
                    ):
                        local_repository_names[owner].add(target.id)
                        changed = True
            potential_repository_type_names = (
                set(repository_names)
                | potential_module_repository_names
                | potential_local_repository_names
            )
            for names in local_repository_names.values():
                potential_repository_type_names.update(names)
            local_alias_events: dict[
                ast.AST, dict[str, list[tuple[tuple[int, int], tuple[str, ...] | None]]]
            ] = {
                owner: _must_alias_events(owner, repository_names, module_names)
                for owner in local_names
                if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for aliases in local_alias_events.values():
                for name, events in aliases.items():
                    if _AUDIT_REPOSITORY_CLASS in {identity for _position, identity in events}:
                        potential_repository_type_names.add(name)
            potential_alias_seed = {
                name: _AUDIT_REPOSITORY_CLASS for name in potential_imported_repository_names
            }
            potential_alias_events: dict[ast.AST | None, _AliasEvents] = {
                None: _must_alias_events(
                    tree,
                    potential_imported_repository_names,
                    {},
                    initial_state=potential_alias_seed,
                    potential_import_aliases=True,
                )
            }
            for owner in local_names:
                if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                initial_state = dict(potential_alias_seed)
                for name in local_names[owner] & potential_imported_repository_names:
                    initial_state[name] = None
                potential_alias_events[owner] = _must_alias_events(
                    owner,
                    potential_imported_repository_names,
                    {},
                    initial_state=initial_state,
                    potential_import_aliases=True,
                )
            typed_repository_attributes: dict[tuple[str, ...], dict[str, frozenset[str]]] = {}
            potential_typed_repository_attributes: dict[tuple[str, ...], set[str]] = {}
            potential_typed_attribute_names: dict[str, set[str]] = {}
            class_identities: set[tuple[str, ...]] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                class_identity = _lexical_owner_path(node, parents)
                class_identities.add(class_identity)
                attributes: dict[str, frozenset[str]] = {}
                for statement in node.body:
                    if not isinstance(statement, ast.AnnAssign) or not isinstance(
                        statement.target, ast.Name
                    ):
                        continue
                    visible_repository_names = _visible_repository_names(
                        _enclosing_function(node, parents),
                        repository_names,
                        local_repository_names,
                        local_names,
                        parents,
                    )
                    operations = _annotation_repository_operations(
                        statement.annotation,
                        visible_repository_names,
                        collaborator_names,
                        module_names,
                    )
                    if operations:
                        attributes[statement.target.id] = operations
                if attributes:
                    potential_typed_attribute_names.setdefault(node.name, set()).update(attributes)
                if isinstance(parents.get(node), ast.ClassDef):
                    if attributes:
                        potential_typed_repository_attributes[class_identity] = set(attributes)
                    continue
                if attributes:
                    typed_repository_attributes[class_identity] = attributes

            bound_paths: dict[tuple[ast.AST | None, tuple[str, ...]], frozenset[str]] = {}
            known_provenance_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            potential_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            control_flow_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            compound_body_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            reviewed_edge_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            reviewed_edges = _AUDIT_REPOSITORY_REVIEWED_COLLABORATOR_EDGES.get(relative_path, {})
            for owner in ast.walk(tree):
                if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                owner_path = _lexical_owner_path(owner, parents)
                for (reviewed_owner_path, receiver), operations in reviewed_edges.items():
                    if owner_path == reviewed_owner_path:
                        bound_paths[(owner, receiver)] = operations
                        reviewed_edge_paths.add((owner, receiver))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visible_repository_names = _visible_annotation_repository_names(
                        node,
                        node,
                        repository_names,
                        local_repository_names,
                        local_names,
                        local_alias_events,
                        parents,
                    )
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    ):
                        operations = _annotation_repository_operations(
                            argument.annotation,
                            visible_repository_names,
                            collaborator_names,
                            module_names,
                        )
                        if operations and argument.arg not in reassigned_names[node]:
                            bound_paths[(node, (argument.arg,))] = operations
                        elif (
                            potential_repository_type_names.intersection(
                                _annotation_names(argument.annotation)
                            )
                            or _visible_potential_annotation_repository_names(
                                node,
                                node,
                                potential_imported_repository_names,
                                local_names,
                                potential_alias_events,
                                parents,
                            ).intersection(_annotation_names(argument.annotation))
                            or _is_potential_repository_annotation(argument.annotation)
                        ):
                            potential_paths.add((node, (argument.arg,)))
                        for annotation_name in _annotation_names(argument.annotation):
                            class_identity = _annotation_class_identity(
                                node, annotation_name, class_identities, parents
                            )
                            for attribute, attribute_operations in typed_repository_attributes.get(
                                class_identity or (), {}
                            ).items():
                                bound_paths[(node, (argument.arg, attribute))] = (
                                    attribute_operations
                                )
                            for attribute in potential_typed_repository_attributes.get(
                                class_identity or (), set()
                            ) | potential_typed_attribute_names.get(annotation_name, set()):
                                potential_paths.add((node, (argument.arg, attribute)))
                if isinstance(node, ast.AnnAssign):
                    annotation_scope = _enclosing_function(node, parents)
                    visible_repository_names = _visible_annotation_repository_names(
                        node,
                        annotation_scope,
                        repository_names,
                        local_repository_names,
                        local_names,
                        local_alias_events,
                        parents,
                    )
                    operations = _annotation_repository_operations(
                        node.annotation,
                        visible_repository_names,
                        collaborator_names,
                        module_names,
                    )
                    target = _dotted_ast_path(node.target)
                    if target is not None and operations:
                        bound_paths[(_provenance_scope(node, target, parents), target)] = operations
                    elif target is not None and (
                        potential_repository_type_names.intersection(
                            _annotation_names(node.annotation)
                        )
                        or _visible_potential_annotation_repository_names(
                            node,
                            annotation_scope,
                            potential_imported_repository_names,
                            local_names,
                            potential_alias_events,
                            parents,
                        ).intersection(_annotation_names(node.annotation))
                        or _is_potential_repository_annotation(node.annotation)
                    ):
                        potential_paths.add((_provenance_scope(node, target, parents), target))
                    if target is not None:
                        for annotation_name in _annotation_names(node.annotation):
                            class_identity = _annotation_class_identity(
                                node, annotation_name, class_identities, parents
                            )
                            for attribute, attribute_operations in typed_repository_attributes.get(
                                class_identity or (), {}
                            ).items():
                                bound_paths[
                                    (_provenance_scope(node, target, parents), (*target, attribute))
                                ] = attribute_operations
                            for attribute in potential_typed_repository_attributes.get(
                                class_identity or (), set()
                            ) | potential_typed_attribute_names.get(annotation_name, set()):
                                potential_paths.add(
                                    (_provenance_scope(node, target, parents), (*target, attribute))
                                )
            changed = True
            while changed:
                changed = False
                for node in ast.walk(tree):
                    for target_node, value in _assignment_bindings(node):
                        target = _dotted_ast_path(target_node)
                        if target is None:
                            continue
                        target_scope = _binding_scope(node, target, parents)
                        control_flow_gated = _inside_unmodeled_control_flow(node, parents)
                        owner = _enclosing_function(node, parents)
                        visible_repository_names = _visible_repository_names(
                            owner,
                            repository_names,
                            local_repository_names,
                            local_names,
                            parents,
                        )
                        value_states: list[
                            tuple[
                                tuple[str, ...] | None,
                                ast.AST | None,
                                frozenset[str] | None,
                                bool,
                                tuple[str, ...] | None,
                                bool,
                                bool,
                            ]
                        ] = []
                        for value_option in _assignment_value_options(value):
                            option_source = (
                                _dotted_ast_path(value_option.func)
                                if isinstance(value_option, ast.Call)
                                and isinstance(value_option.func, ast.Name)
                                else _dotted_ast_path(value_option)
                            )
                            option_scope = _provenance_scope(node, option_source, parents)
                            option_operations = _bound_repository_operations(
                                bound_paths,
                                owner
                                if option_source and option_source[0] == "self"
                                else option_scope,
                                option_source,
                                parents,
                                local_names,
                                competing_names,
                            )
                            factory_identity, factory_is_shadowed = (
                                _resolved_scoped_factory(
                                    value_option.func,
                                    owner,
                                    repository_names,
                                    module_names,
                                    local_alias_events,
                                    local_names,
                                    parents,
                                )
                                if isinstance(value_option, ast.Call)
                                else (None, False)
                            )
                            if factory_identity == (*_AUDIT_REPOSITORY_CLASS, "scoped"):
                                option_operations = _AUDIT_REPOSITORY_OPERATION_NAMES
                            value_states.append(
                                (
                                    option_source,
                                    option_scope,
                                    option_operations,
                                    _has_potential_repository_path(
                                        potential_paths, option_scope, option_source, parents
                                    ),
                                    factory_identity,
                                    factory_is_shadowed,
                                    isinstance(value_option, ast.Call)
                                    and isinstance(value_option.func, ast.Name)
                                    and value_option.func.id in ambiguous_module_names,
                                )
                            )
                        sources = [state[0] for state in value_states]
                        source = sources[0] if all(item == sources[0] for item in sources) else None
                        source_scope = _provenance_scope(node, source, parents)
                        branch_operations = [state[2] for state in value_states]
                        operations = (
                            branch_operations[0]
                            if branch_operations[0] is not None
                            and all(item == branch_operations[0] for item in branch_operations)
                            else None
                        )
                        source_is_potential = operations is None and any(
                            state[2] is not None or state[3] or state[5] or state[6]
                            for state in value_states
                        )
                        factory_identity = (
                            value_states[0][4]
                            if all(state[4] == value_states[0][4] for state in value_states)
                            else None
                        )
                        shadowed_scoped_factory = any(state[5] for state in value_states)
                        if isinstance(node, ast.NamedExpr):
                            source_is_potential = False
                        source_is_compound_shadowed = (
                            source is not None
                            and (owner, source) not in reviewed_edge_paths
                            and _compound_body_shadowed(node, source[0], parents)
                            and (
                                operations is not None
                                or source_is_potential
                                or factory_identity == (*_AUDIT_REPOSITORY_CLASS, "scoped")
                                or shadowed_scoped_factory
                            )
                        )
                        if source_is_compound_shadowed:
                            source_is_reviewed_edge = (owner, source) in reviewed_edge_paths
                            if not source_is_reviewed_edge:
                                operations = None
                                source_is_potential = True
                                bound_paths.pop((target_scope, target), None)
                            compound_body_paths.add((target_scope, target))
                        ambiguous_module_factory = any(state[6] for state in value_states)
                        if control_flow_gated:
                            source_is_reviewed_edge = (owner, source) in reviewed_edge_paths
                            if (
                                source_is_reviewed_edge
                                and operations
                                and (target_scope, target) not in bound_paths
                            ):
                                bound_paths[(target_scope, target)] = operations
                                changed = True
                            if (
                                operations
                                or source_is_potential
                                or shadowed_scoped_factory
                                or ambiguous_module_factory
                            ) and (target_scope, target) not in potential_paths:
                                potential_paths.add((target_scope, target))
                                if not source_is_reviewed_edge:
                                    control_flow_paths.add((target_scope, target))
                                changed = True
                            continue
                        if (
                            operations
                            and (target_scope, target) not in bound_paths
                            and (target_scope, target) not in known_provenance_paths
                        ):
                            bound_paths[(target_scope, target)] = operations
                            known_provenance_paths.add((target_scope, target))
                            changed = True
                        elif (
                            operations is None
                            and (target_scope, target) in known_provenance_paths
                            and not (
                                (target_scope, target) in reviewed_edge_paths
                                and isinstance(value, ast.Call)
                                and isinstance(value.func, ast.Name)
                                and value.func.id == "getattr"
                            )
                        ):
                            if bound_paths.pop((target_scope, target), None) is not None:
                                changed = True
                        if (
                            source_is_potential
                            or shadowed_scoped_factory
                            or ambiguous_module_factory
                        ) and (target_scope, target) not in potential_paths:
                            potential_paths.add((target_scope, target))
                            changed = True
                    continue

            invalidated_paths: set[tuple[ast.AST | None, tuple[str, ...]]] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target_node = node.targets[0]
                    value = node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    target_node = node.target
                    value = node.value
                else:
                    continue
                target = _dotted_ast_path(target_node)
                if target is None:
                    continue
                target_scope = _binding_scope(node, target, parents)
                target_key = (target_scope, target)
                if target_key in reviewed_edge_paths:
                    continue
                if len(target) == 1 or target_key not in bound_paths:
                    continue
                source = _dotted_ast_path(value)
                owner = _enclosing_function(node, parents)
                operations = _bound_repository_operations(
                    bound_paths,
                    _provenance_scope(node, source, parents),
                    source,
                    parents,
                    local_names,
                    competing_names,
                )
                if isinstance(value, ast.Call):
                    visible_repository_names = _visible_repository_names(
                        owner,
                        repository_names,
                        local_repository_names,
                        local_names,
                        parents,
                    )
                    factory_identity, _shadowed = _resolved_scoped_factory(
                        value.func,
                        owner,
                        visible_repository_names,
                        module_names,
                        local_alias_events,
                        local_names,
                        parents,
                    )
                    if factory_identity == (*_AUDIT_REPOSITORY_CLASS, "scoped"):
                        operations = _AUDIT_REPOSITORY_OPERATION_NAMES
                if operations is None:
                    invalidated_paths.add(target_key)
                    potential_paths.add(target_key)

            suspicious_receivers: set[tuple[ast.AST | None, tuple[str, ...]]] = {
                (_provenance_scope(node, dotted, parents), dotted)
                for node in ast.walk(tree)
                if (dotted := _dotted_ast_path(node)) is not None
                and dotted[-1] == "audit_repository"
            }
            changed = True
            while changed:
                changed = False
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign) and len(node.targets) == 1:
                        target_node = node.targets[0]
                        value = node.value
                    elif isinstance(node, ast.AnnAssign) and node.value is not None:
                        target_node = node.target
                        value = node.value
                    else:
                        continue
                    target = _dotted_ast_path(target_node)
                    source = _dotted_ast_path(value)
                    if target is None or source is None:
                        continue
                    target_key = (_binding_scope(node, target, parents), target)
                    source_scope = _provenance_scope(node, source, parents)
                    source_is_suspicious = any(
                        (candidate_scope, source) in suspicious_receivers
                        for candidate_scope in _provenance_scope_chain(source_scope, parents)
                    )
                    if source_is_suspicious and target_key not in suspicious_receivers:
                        suspicious_receivers.add(target_key)
                        changed = True
            provenance_receiver_paths = {receiver for _scope, receiver in bound_paths}
            for node in ast.walk(tree):
                if (
                    not isinstance(node, ast.Call)
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr not in _AUDIT_REPOSITORY_OPERATION_NAMES
                ):
                    continue
                receiver = _dotted_ast_path(node.func.value)
                owner = _enclosing_function(node, parents)
                owner_name = owner.name if owner is not None else "<module>"
                call = f"{relative_path}::{owner_name}::{node.func.attr}"
                call_site = (call, receiver, node.lineno, node.col_offset)
                scope = _provenance_scope(node, receiver, parents)
                operations = _bound_repository_operations(
                    bound_paths,
                    scope,
                    receiver,
                    parents,
                    local_names,
                    competing_names,
                    invalidated_paths,
                )
                declaration_scope = _binding_scope(node, receiver, parents)
                if (
                    (owner, receiver) in reassigned_paths
                    or (declaration_scope, receiver) in declared_reassigned_paths
                    or _is_expression_shadowed(node, receiver, parents)
                    or (scope, receiver) in control_flow_paths
                    or (scope, receiver) in compound_body_paths
                ):
                    operations = None
                is_name_signaled = any(
                    (candidate_scope, receiver) in suspicious_receivers
                    for candidate_scope in _provenance_scope_chain(scope, parents)
                )
                is_reviewed_receiver = (owner, receiver) in reviewed_edge_paths
                is_potential = (
                    operations is not None
                    or is_name_signaled
                    or is_reviewed_receiver
                    or receiver in provenance_receiver_paths
                    or _has_potential_repository_path(potential_paths, scope, receiver, parents)
                    or _has_potential_repository_path(
                        known_provenance_paths, scope, receiver, parents
                    )
                )
                if is_potential:
                    potential.add(call_site)
                has_reviewed_provenance = node.func.attr in (operations or frozenset())
                if has_reviewed_provenance:
                    reviewed.add(call_site)
    return frozenset(reviewed), frozenset(potential)


def _audit_repository_public_call_inventory(root: Path) -> frozenset[str]:
    """Conservatively inventory calls rooted in a known owner-bound repository value."""
    reviewed, _potential = _audit_repository_public_call_provenance(root)
    return frozenset(call for call, _receiver, _line, _column in reviewed)


def _unreviewed_audit_repository_public_calls(root: Path) -> frozenset[str]:
    """Return candidate calls that lack independently reviewed provenance."""
    reviewed, potential = _audit_repository_public_call_provenance(root)
    unreviewed = potential - reviewed
    return frozenset(call for call, _receiver, _line, _column in unreviewed)


def _audit_repository_public_call_inventories(
    root: Path,
) -> tuple[frozenset[str], frozenset[str]]:
    """Derive reviewed and unreviewed inventories from one production-tree scan."""
    reviewed, potential = _audit_repository_public_call_provenance(root)
    reviewed_inventory = frozenset(call for call, _receiver, _line, _column in reviewed)
    unreviewed_inventory = frozenset(
        call for call, _receiver, _line, _column in potential - reviewed
    )
    return reviewed_inventory, unreviewed_inventory


_AUDIT_REPOSITORY_PUBLIC_CALL_INVENTORY = frozenset(
    {
        "examples/04_native_tool.py::main::list_by_run",
        "examples/21_policy_block.py::main::list_by_run",
        "examples/24_audit_query.py::main::list",
        "examples/24_audit_query.py::main::list_by_run",
        "src/zeroth/governance/approvals/service.py::_record_api_audit::write",
        "src/zeroth/governance/approvals/service.py::_record_decision_audit::write",
        "src/zeroth/governance/audit/delivery_worker.py::_attempt::write",
        "src/zeroth/governance/audit/verifier.py::verify_deployment::list_by_deployment",
        "src/zeroth/governance/audit/verifier.py::verify_run::list_by_run",
        "src/zeroth/governance/retention/erasure_service.py::erase_run::crypto_erase_in_transaction",
        "src/zeroth/governance/retention/erasure_service.py::erase_run::list_by_run",
        "src/zeroth/governance/retention/erasure_service.py::erase_run::list_by_run_in_transaction",
        "src/zeroth/governance/retention/erasure_service.py::purge_audits::crypto_erase_in_transaction",
        "src/zeroth/governance/retention/erasure_service.py::purge_audits::list_erasable_in_transaction",
        "src/zeroth/runtime/orchestration/audit_recorder.py::record_failed_branch_execution::write",
        "src/zeroth/runtime/orchestration/audit_recorder.py::record_failed_execution::write",
        "src/zeroth/runtime/orchestration/audit_recorder.py::record_history::write",
        "src/zeroth/runtime/orchestration/audit_recorder.py::record_policy_rejection::write",
        "src/zeroth/runtime/orchestration/parallel_executor.py::branch_coro_factory::write",
        "src/zeroth/runtime/orchestration/run_worker.py::_record_worker_audit::write",
        "src/zeroth/service/api/audit_api.py::_verify_run_chain::list_by_run",
        "src/zeroth/service/api/audit_api.py::get_deployment_evidence::list_by_deployment",
        "src/zeroth/service/api/audit_api.py::get_deployment_timeline::list_by_deployment",
        "src/zeroth/service/api/audit_api.py::get_run_evidence::list_by_run",
        "src/zeroth/service/api/audit_api.py::get_run_timeline::list_by_run",
        "src/zeroth/service/api/audit_api.py::list_audits::list",
        "src/zeroth/service/api/authentication.py::record_service_denial::write",
        "src/zeroth/service/api/econ_analytics_api.py::_windowed_runs_and_audits::list",
        "src/zeroth/service/api/retention_api.py::_erase_tenant::list_erasable",
        "src/zeroth/service/api/retention_api.py::_require_run_tenant::list",
        "src/zeroth/service/api/rightsizing_api.py::rightsizing_opportunities::list",
        "src/zeroth/service/api/rightsizing_api.py::run_rightsizing_experiment::list",
    }
)


def test_production_audit_repository_public_calls_are_exhaustive_and_reviewed() -> None:
    root = Path(__file__).resolve().parents[2]
    # The binding inventory independently prohibits direct/default construction,
    # so every operation below is rooted in the sole reviewed scoped factory.
    assert _audit_repository_binding_inventory(root) == frozenset(
        {"src/zeroth/service/bootstrap/factory.py::bootstrap_service::scoped"}
    )
    reviewed, unreviewed = _audit_repository_public_call_inventories(root)
    assert reviewed == _AUDIT_REPOSITORY_PUBLIC_CALL_INVENTORY
    assert unreviewed == frozenset()


def test_public_call_inventory_tracks_scoped_factory_and_instance_aliases(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository as Repo\n"
        "factory = Repo.scoped\n"
        "async def use():\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_binding_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::scoped"}
    )
    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_tracks_local_scoped_factory_alias_chains(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def via_class(record):\n"
        "    Repo = AuditRepository\n"
        "    repository = Repo.scoped(db, scope)\n"
        "    await repository.write(record)\n"
        "async def via_factory(record):\n"
        "    factory = AuditRepository.scoped\n"
        "    alias = factory\n"
        "    repository = alias(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {
            "apps/candidate.py::via_class::write",
            "apps/candidate.py::via_factory::write",
        }
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_rejects_rebound_local_scoped_factory(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    factory = AuditRepository.scoped\n"
        "    factory = other_factory\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_rebound_local_type_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(candidate):\n"
        "    Repo = AuditRepository\n"
        "    Repo = OtherRepository\n"
        "    repository: Repo = candidate\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_outer_rebound_type_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "def outer():\n"
        "    Repo = AuditRepository\n"
        "    Repo = OtherRepository\n"
        "    async def use(repository: Repo):\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "rebind",
    [
        "    try:\n        factory = other_factory\n    except Exception:\n        pass\n",
        "    match choice:\n        case _:\n            factory = other_factory\n",
        "    import other as factory\n",
        "    def factory():\n        pass\n",
        "    async def factory():\n        pass\n",
        "    for factory in factories:\n        pass\n",
        "    async for factory in factories:\n        pass\n",
        "    with resource as factory:\n        pass\n",
        "    if factory := other_factory:\n        pass\n",
    ],
)
def test_public_call_inventory_rejects_unmodeled_factory_rebinding(
    tmp_path: Path, rebind: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(choice, factories, resource, record):\n"
        "    factory = AuditRepository.scoped\n"
        f"{rebind}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "rebind",
    [
        "    for factory in factories:\n        pass\n",
        "    with manager() as factory:\n        pass\n",
        "    try:\n        raise ValueError\n    except Exception as factory:\n        pass\n",
    ],
)
def test_public_call_inventory_rejects_binding_form_factory_rebinding(
    tmp_path: Path, rebind: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(factories, record):\n"
        "    factory = AuditRepository.scoped\n"
        f"{rebind}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "compound",
    [
        "    for factory in factories:\n"
        "        repository = factory(db, scope)\n"
        "        await repository.write(record)\n",
        "    with manager() as factory:\n"
        "        repository = factory(db, scope)\n"
        "        await repository.write(record)\n",
    ],
)
def test_public_call_inventory_invalidates_factory_at_compound_body_entry(
    tmp_path: Path, compound: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(factories, record):\n"
        "    factory = AuditRepository.scoped\n"
        f"{compound}",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_decomposes_instance_assignments(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    repo = alias = AuditRepository.scoped(db, scope)\n"
        "    first, unrelated = (AuditRepository.scoped(db, scope), object())\n"
        "    await repo.write(record)\n"
        "    await alias.write(record)\n"
        "    await first.write(record)\n"
        "    await (walrus := AuditRepository.scoped(db, scope)).write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_reports_unrelated_decomposed_assignments(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    repo = alias = AuditRepository.scoped(db, scope)\n"
        "    first, unrelated = (AuditRepository.scoped(db, scope), object())\n"
        "    walrus = AuditRepository.scoped(db, scope)\n"
        "    repo = alias = client\n"
        "    first, unrelated = (client, object())\n"
        "    await repo.write(record)\n"
        "    await alias.write(record)\n"
        "    await first.write(record)\n"
        "    await (walrus := client).write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_recursively_decomposes_instance_assignments(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    repo, (alias, other) = (\n"
        "        AuditRepository.scoped(db, scope),\n"
        "        (AuditRepository.scoped(db, scope), object()),\n"
        "    )\n"
        "    await repo.write(record)\n"
        "    await alias.list_by_run(record.run_id)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {
            "apps/candidate.py::use::list_by_run",
            "apps/candidate.py::use::write",
        }
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_recursively_invalidates_instance_assignments(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    repo, (alias, other) = (\n"
        "        AuditRepository.scoped(db, scope),\n"
        "        (AuditRepository.scoped(db, scope), object()),\n"
        "    )\n"
        "    repo, (alias, other) = (client, (client, object()))\n"
        "    await repo.write(record)\n"
        "    await alias.list_by_run(record.run_id)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {
            "apps/candidate.py::use::list_by_run",
            "apps/candidate.py::use::write",
        }
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "repo, *rest = (AuditRepository.scoped(db, scope), object())",
        "first, *middle, repo = (object(), object(), AuditRepository.scoped(db, scope))",
        "first, (*middle, repo) = (object(), (object(), AuditRepository.scoped(db, scope)))",
    ],
)
def test_public_call_inventory_decomposes_statically_sized_starred_assignments(
    tmp_path: Path, assignment: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        f"    {assignment}\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_joins_trusted_conditional_assignment(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record, cond):\n"
        "    repo = (\n"
        "        AuditRepository.scoped(db, scope)\n"
        "        if cond\n"
        "        else AuditRepository.scoped(db, scope)\n"
        "    )\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_reports_mixed_conditional_assignment(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record, cond):\n"
        "    repo = AuditRepository.scoped(db, scope) if cond else client\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    ("target", "alternative", "trusted"),
    [
        ("repo, other", "AuditRepository.scoped(db, scope)", True),
        ("repo, other", "client", False),
        ("repo, *rest", "AuditRepository.scoped(db, scope)", True),
        ("repo, *rest", "client", False),
    ],
)
def test_public_call_inventory_composes_structured_conditional_assignments(
    tmp_path: Path, target: str, alternative: str, trusted: bool
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record, cond):\n"
        f"    {target} = (\n"
        "        (AuditRepository.scoped(db, scope), object())\n"
        "        if cond\n"
        f"        else ({alternative}, object())\n"
        "    )\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    call = "apps/candidate.py::use::write"
    assert _audit_repository_public_call_inventory(tmp_path) == (
        frozenset({call}) if trusted else frozenset()
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == (
        frozenset() if trusted else frozenset({call})
    )


def test_public_call_inventory_tracks_current_local_type_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(candidate):\n"
        "    Repo = AuditRepository\n"
        "    repository: Repo = candidate\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_does_not_leak_module_attributes_to_local_class(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "class Holder:\n"
        "    repository: AuditRepository\n"
        "async def use(candidate):\n"
        "    class Holder:\n"
        "        pass\n"
        "    holder: Holder = candidate\n"
        "    await holder.repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize("nested", ["def", "async def"])
def test_public_call_inventory_rejects_nested_definition_rebinding_parameter(
    tmp_path: Path, nested: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository):\n"
        f"    {nested} repository():\n"
        "        pass\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "branches",
    [
        (
            "    if condition:\n"
            "        factory = other_factory\n"
            "    else:\n"
            "        factory = AuditRepository.scoped\n"
        ),
        (
            "    if condition:\n"
            "        factory = AuditRepository.scoped\n"
            "    else:\n"
            "        factory = other_factory\n"
        ),
    ],
)
def test_public_call_inventory_rejects_branch_divergent_scoped_factory(
    tmp_path: Path, branches: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(condition, record):\n"
        f"{branches}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_tracks_branch_joined_scoped_factory(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(condition, record):\n"
        "    if condition:\n"
        "        factory = AuditRepository.scoped\n"
        "    else:\n"
        "        factory = AuditRepository.scoped\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize(
    "annotation",
    [
        "AuditRepository | None",
        "Annotated[AuditRepository, marker]",
        "Annotated[unrelated.AuditRepository, marker]",
    ],
)
def test_public_call_inventory_gates_unresolved_repository_type_positions(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from typing import Annotated\n"
        "from my_adapter import AuditRepository\n"
        "import unrelated\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "annotation",
    [
        "Optional[unrelated.AuditRepository]",
        '"Optional[unrelated.AuditRepository]"',
        "Annotated[unrelated.AuditRepository | None, marker]",
    ],
)
def test_public_call_inventory_reports_nested_unresolved_repository_annotations(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from typing import Annotated, Optional\n"
        "from zeroth.governance.audit import AuditRepository\n"
        "import unrelated\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "setup",
    [
        "    if condition:\n        factory = AuditRepository.scoped\n",
        "    factory = AuditRepository.scoped\n    for _ in records:\n        factory = other_factory\n",
    ],
)
def test_public_call_inventory_rejects_conditionally_rebound_scoped_factory(
    tmp_path: Path, setup: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(condition, records, record):\n"
        f"{setup}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "setup",
    [
        "    factory = AuditRepository.scoped\n"
        "    try:\n"
        "        factory = other_factory\n"
        "    except Exception:\n"
        "        pass\n",
        "    for _ in records:\n"
        "        break\n"
        "    else:\n"
        "        factory = AuditRepository.scoped\n",
    ],
)
def test_public_call_inventory_rejects_exceptional_or_breakable_factory_state(
    tmp_path: Path, setup: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(records, record):\n"
        f"{setup}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_tracks_agreeing_try_factory_paths(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(condition, record):\n"
        "    try:\n"
        "        if condition:\n"
        "            factory = AuditRepository.scoped\n"
        "        else:\n"
        "            factory = AuditRepository.scoped\n"
        "    finally:\n"
        "        factory = AuditRepository.scoped\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_reports_ambiguous_try_factory_paths(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(condition, record):\n"
        "    try:\n"
        "        if condition:\n"
        "            factory = AuditRepository.scoped\n"
        "        else:\n"
        "            factory = other_factory\n"
        "    except Exception:\n"
        "        factory = other_factory\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_tracks_exhaustive_scoped_match_factory_paths(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(kind, record):\n"
        "    match kind:\n"
        "        case 'scheduled':\n"
        "            factory = AuditRepository.scoped\n"
        "        case _:\n"
        "            factory = AuditRepository.scoped\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_reports_ambiguous_match_factory_paths(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(kind, record):\n"
        "    match kind:\n"
        "        case 'scheduled':\n"
        "            factory = AuditRepository.scoped\n"
        "        case _:\n"
        "            factory = other_factory\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "annotation",
    ["unrelated.AuditRepository", '"unrelated.AuditRepository"'],
)
def test_public_call_inventory_rejects_unrelated_qualified_annotation(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "source",
    [
        "async def use(Repo, candidate):\n"
        "        repository: Repo = candidate\n"
        "        await repository.write(record)\n",
        "async def use(candidate):\n"
        "        Repo = OtherRepository\n"
        "        repository: Repo = candidate\n"
        "        await repository.write(record)\n",
    ],
)
def test_public_call_inventory_rejects_inner_type_alias_shadow(tmp_path: Path, source: str) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "def outer():\n"
        "    Repo = AuditRepository\n"
        f"    {source}",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_shadowed_repository_class_name(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(AuditRepository, record):\n"
        "    repository = AuditRepository.scoped(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_rebound_module_factory_and_type_aliases(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "factory = AuditRepository.scoped\n"
        "factory = other\n"
        "Repo = AuditRepository\n"
        "Repo = OtherRepository\n"
        "async def from_factory(record):\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n"
        "async def from_annotation(repository: Repo):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {
            "apps/candidate.py::from_annotation::write",
            "apps/candidate.py::from_factory::write",
        }
    )


def test_public_call_inventory_rejects_import_and_except_rebinding(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def imported(repository: AuditRepository):\n"
        "    import other as repository\n"
        "    await repository.write(record)\n"
        "async def handled(repository: AuditRepository):\n"
        "    try:\n"
        "        raise ValueError\n"
        "    except Exception as repository:\n"
        "        pass\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {
            "apps/candidate.py::handled::write",
            "apps/candidate.py::imported::write",
        }
    )


def test_public_call_inventory_rejects_except_star_factory_rebinding(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    factory = AuditRepository.scoped\n"
        "    try:\n"
        "        raise ExceptionGroup('failure', [ValueError()])\n"
        "    except* ValueError:\n"
        "        factory = other_factory\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_honors_lambda_parameter_shadowing(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, records):\n"
        "    callbacks = [lambda repository: repository.write(record)]\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_keeps_typed_repository_outside_lambda_walrus_scope(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, record):\n"
        "    callback = lambda: (repository := other_repository)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_scopes_nested_lambda_default_walrus_to_outer_lambda(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, record):\n"
        "    callback = lambda: (lambda value=(repository := other_repository): value)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize(
    "annotation",
    ["list[AuditRepository]", "Callable[[AuditRepository], None]"],
)
def test_public_call_inventory_rejects_generic_repository_type_positions(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from collections.abc import Callable\n"
        "from zeroth.governance.audit import AuditRepository\n"
        f"async def use(holder: {annotation}, record):\n"
        "    await holder.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "annotation",
    [
        "typing.Optional[AuditRepository]",
        "typing.Union[AuditRepository, None]",
        "typing.Union[None, AuditRepository]",
    ],
)
def test_public_call_inventory_unwraps_typing_repository_wrappers(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import typing\n"
        "from zeroth.governance.audit import AuditRepository\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize(
    ("assignment", "expected", "unreviewed"),
    [
        (
            "factory = AuditRepository.scoped if cond else AuditRepository.scoped",
            frozenset({"apps/candidate.py::use::write"}),
            frozenset(),
        ),
        (
            "factory = AuditRepository.scoped if cond else other_factory",
            frozenset(),
            frozenset({"apps/candidate.py::use::write"}),
        ),
    ],
)
def test_public_call_inventory_joins_conditional_factory_aliases(
    tmp_path: Path,
    assignment: str,
    expected: frozenset[str],
    unreviewed: frozenset[str],
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record, cond):\n"
        f"    {assignment}\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == expected
    assert _unreviewed_audit_repository_public_calls(tmp_path) == unreviewed


@pytest.mark.parametrize(
    ("assignment", "expected", "unreviewed"),
    [
        (
            "Repo = AuditRepository if cond else AuditRepository",
            frozenset({"apps/candidate.py::use::write"}),
            frozenset(),
        ),
        (
            "Repo = AuditRepository if cond else OtherRepository",
            frozenset(),
            frozenset({"apps/candidate.py::use::write"}),
        ),
    ],
)
def test_public_call_inventory_joins_conditional_type_aliases(
    tmp_path: Path,
    assignment: str,
    expected: frozenset[str],
    unreviewed: frozenset[str],
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(candidate, record, cond):\n"
        f"    {assignment}\n"
        "    repository: Repo = candidate\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == expected
    assert _unreviewed_audit_repository_public_calls(tmp_path) == unreviewed


def test_public_call_inventory_reports_nested_divergent_conditional_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record, cond, other):\n"
        "    factory = (AuditRepository.scoped if cond else other) if cond else other\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_reports_mixed_structured_conditional_branch(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record, cond, clients):\n"
        "    repo, other = (AuditRepository.scoped(db, scope), object()) if cond else clients\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "annotation",
    ["list[AuditRecordWriter]", "Callable[[AuditRecordWriter], None]"],
)
def test_public_call_inventory_rejects_generic_collaborator_type_positions(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from collections.abc import Callable\n"
        "from zeroth.governance.audit.delivery_state import AuditRecordWriter\n"
        f"async def use(writer: {annotation}, record):\n"
        "    await writer.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize("local", [False, True])
def test_public_call_inventory_propagates_current_unresolved_repository_alias(
    tmp_path: Path, local: bool
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    prefix = (
        "async def use(record):\n"
        "    from my_adapter import AuditRepository as AR\n"
        "    Alias = AR\n"
        "    repository: Alias = candidate\n"
        if local
        else "from my_adapter import AuditRepository as AR\n"
        "Alias = AR\n"
        "async def use(repository: Alias, record):\n"
    )
    module.write_text(
        prefix + "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize("local", [False, True])
def test_public_call_inventory_invalidates_rebound_unresolved_repository_alias(
    tmp_path: Path, local: bool
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    prefix = (
        "async def use(record):\n"
        "    from my_adapter import AuditRepository as AR\n"
        "    Alias = AR\n"
        "    Alias = OtherRepository\n"
        "    repository: Alias = candidate\n"
        if local
        else "from my_adapter import AuditRepository as AR\n"
        "Alias = AR\n"
        "Alias = OtherRepository\n"
        "async def use(repository: Alias, record):\n"
    )
    module.write_text(
        prefix + "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize("annotation", ["AR", "'AR'"])
def test_public_call_inventory_reports_unresolved_imported_repository_alias(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from my_adapter import AuditRepository as AR\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_reports_local_unresolved_repository_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "async def use(record):\n"
        "    from my_adapter import AuditRepository as AR\n"
        "    repository: AR = candidate\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_match_capture_factory_rebinding(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(choice, record):\n"
        "    factory = AuditRepository.scoped\n"
        "    match choice:\n"
        "        case factory:\n"
        "            pass\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "pattern",
    ["repository", "{'repository': repository}"],
)
def test_public_call_inventory_rejects_match_capture_repository_rebinding(
    tmp_path: Path, pattern: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, candidate, record):\n"
        "    match candidate:\n"
        f"        case {pattern}:\n"
        "            pass\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_match_guard_factory_rebinding(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(choice, record):\n"
        "    factory = AuditRepository.scoped\n"
        "    match choice:\n"
        "        case _ if (factory := other_factory):\n"
        "            pass\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_match_capture_in_guard(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, choice, record):\n"
        "    match choice:\n"
        "        case repository if repository.write(record):\n"
        "            pass\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "expression",
    [
        "    result = (factory := other_factory)\n",
        "    consume(factory := other_factory)\n",
    ],
)
def test_public_call_inventory_rejects_general_named_expression_factory_rebinding(
    tmp_path: Path, expression: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    factory = AuditRepository.scoped\n"
        f"{expression}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    ("expression", "reviewed", "unreviewed"),
    [
        (
            "    callback = lambda: (factory := other_factory)\n",
            frozenset({"apps/candidate.py::use::write"}),
            frozenset(),
        ),
        (
            "    callback = lambda value=(factory := other_factory): value\n",
            frozenset(),
            frozenset({"apps/candidate.py::use::write"}),
        ),
    ],
)
def test_public_call_inventory_scopes_lambda_named_expression_bindings(
    tmp_path: Path,
    expression: str,
    reviewed: frozenset[str],
    unreviewed: frozenset[str],
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    factory = AuditRepository.scoped\n"
        f"{expression}"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == reviewed
    assert _unreviewed_audit_repository_public_calls(tmp_path) == unreviewed


def test_public_call_inventory_invalidates_comprehension_named_expression_binding(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(records, record):\n"
        "    factory = AuditRepository.scoped\n"
        "    callbacks = [(factory := other_factory) for _ in records]\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_keeps_lambda_default_provenance(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, record):\n"
        "    callback = lambda repository=repository.write(record): repository\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize(
    "nested",
    [
        "    async def inner(*repository):\n        await repository.write(record)\n",
        "    async def inner(**repository):\n        await repository.write(record)\n",
        "    callback = lambda *repository: repository.write(record)\n",
        "    callback = lambda **repository: repository.write(record)\n",
    ],
)
def test_public_call_inventory_honors_variadic_parameter_shadowing(
    tmp_path: Path, nested: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, record):\n" + nested,
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {
            "apps/candidate.py::inner::write"
            if "inner" in nested
            else "apps/candidate.py::use::write"
        }
    )


def test_public_call_inventory_honors_nonlocal_and_global_bindings(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "repository = client\n"
        "async def outer(repository: AuditRepository, record):\n"
        "    async def captured():\n"
        "        nonlocal repository\n"
        "        await repository.write(record)\n"
        "    async def globalized():\n"
        "        global repository\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::captured::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::globalized::write"}
    )


def test_public_call_inventory_invalidates_global_repository_assignment(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "repository: AuditRepository\n"
        "async def use(client, record):\n"
        "    global repository\n"
        "    repository = client\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_tracks_global_repository_capture(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "repository: AuditRepository\n"
        "async def use(record):\n"
        "    global repository\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_invalidates_nonlocal_repository_assignment(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def outer(repository: AuditRepository, client, record):\n"
        "    async def inner():\n"
        "        nonlocal repository\n"
        "        repository = client\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::inner::write"}
    )


def test_public_call_inventory_gates_factory_bindings_inside_and_after_with(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(record):\n"
        "    with manager():\n"
        "        factory = AuditRepository.scoped\n"
        "        repository = factory(db, scope)\n"
        "        await repository.write(record)\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "block",
    [
        "    try:\n"
        "        factory = AuditRepository.scoped\n"
        "        repository = factory(db, scope)\n"
        "        await repository.write(record)\n"
        "    except Exception:\n"
        "        pass\n",
        "    match choice:\n"
        "        case _:\n"
        "            factory = AuditRepository.scoped\n"
        "            repository = factory(db, scope)\n"
        "            await repository.write(record)\n",
    ],
)
def test_public_call_inventory_gates_factory_bindings_inside_unmodeled_blocks(
    tmp_path: Path, block: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(choice, record):\n"
        f"{block}",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_honors_comprehension_target_shadowing(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, records):\n"
        "    return [repository.write(record) for repository in records]\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_keeps_comprehension_iterable_provenance(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository):\n"
        "    return [record for repository in repository.list()]\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::list"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_invalidates_rebound_reviewed_attribute(tmp_path: Path) -> None:
    module = tmp_path / "src" / "zeroth" / "governance" / "audit" / "verifier.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class AuditContinuityVerifier:\n"
        "    async def verify_run(self, client):\n"
        "        self._repository = client\n"
        "        await self._repository.list_by_run(run_id)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"src/zeroth/governance/audit/verifier.py::verify_run::list_by_run"}
    )


@pytest.mark.parametrize(
    "rebind",
    [
        "        for self._repository in clients:\n            pass\n",
        "        with manager() as self._repository:\n            pass\n",
        "        self._repository += other\n",
    ],
)
def test_public_call_inventory_invalidates_reviewed_attribute_binding_forms(
    tmp_path: Path, rebind: str
) -> None:
    module = tmp_path / "src" / "zeroth" / "governance" / "audit" / "verifier.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class AuditContinuityVerifier:\n"
        "    async def verify_run(self, clients):\n"
        f"{rebind}"
        "        await self._repository.list_by_run(run_id)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"src/zeroth/governance/audit/verifier.py::verify_run::list_by_run"}
    )


@pytest.mark.parametrize(
    "annotation",
    [
        '"AuditRepository"',
        '"zeroth.governance.audit.AuditRepository"',
        '"Annotated[AuditRepository, scope_marker]"',
    ],
)
def test_public_call_inventory_tracks_repository_forward_references(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


@pytest.mark.parametrize(
    "annotation",
    ["unrelated.AuditRepository", '"unrelated.AuditRepository"'],
)
def test_public_call_inventory_rejects_unrelated_qualified_repository_annotation(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import unrelated\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


@pytest.mark.parametrize(
    "annotation",
    ["zeroth.governance.audit.AuditRepository", '"zeroth.governance.audit.AuditRepository"'],
)
def test_public_call_inventory_tracks_fully_qualified_repository_annotation(
    tmp_path: Path, annotation: str
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import zeroth.governance.audit\n"
        f"async def use(repository: {annotation}, record):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_ignores_arbitrary_annotation_strings(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        'async def use(repository: "AuditRepository client", record):\n'
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset()


def test_public_call_inventory_does_not_leak_local_imported_type_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "def define_alias():\n"
        "    from zeroth.governance.audit import AuditRepository as Repo\n"
        "async def unrelated(repository: Repo):\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::unrelated::write"}
    )


def test_public_call_inventory_does_not_transfer_nested_class_attributes(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "class Holder:\n"
        "    repository: object\n"
        "class Outer:\n"
        "    class Holder:\n"
        "        repository: AuditRepository\n"
        "async def unrelated(holder: Holder):\n"
        "    await holder.repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::unrelated::write"}
    )


def test_public_call_inventory_scopes_typed_bindings_to_their_function(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def reviewed(repo: AuditRepository, record):\n"
        "    await repo.write(record)\n"
        "async def unrelated(repo, record):\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::reviewed::write"}
    )


def test_public_call_inventory_does_not_trust_attribute_name_alone(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "async def use(holder, record):\n    await holder.audit_repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()


def test_public_call_inventory_rejects_unreviewed_collaborator_calls(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "async def use(holder, record):\n    await holder.audit_repository.write(record)\n",
        encoding="utf-8",
    )

    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_unreviewed_call_gate_keeps_same_function_call_sites_distinct(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(holder, record):\n"
        "    repository = AuditRepository.scoped(db, scope)\n"
        "    await repository.write(record)\n"
        "    await holder.audit_repository.write(record)\n",
        encoding="utf-8",
    )

    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_scopes_factory_provenance_to_its_function(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "factory = AuditRepository.scoped\n"
        "async def trusted(record):\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n"
        "async def unrelated(factory, record):\n"
        "    repository = factory(db, scope)\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::trusted::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::unrelated::write"}
    )


def test_public_call_inventory_scopes_annotations_to_their_class_method(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "class Trusted:\n"
        "    async def persist(self, repository: AuditRepository, record):\n"
        "        await repository.write(record)\n"
        "class Unrelated:\n"
        "    async def discard(self, repository, record):\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::persist::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::discard::write"}
    )


def test_public_call_inventory_honors_closure_capture_and_shadowing(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def outer(repository: AuditRepository, record):\n"
        "    async def captured():\n"
        "        await repository.write(record)\n"
        "    async def shadowed(repository):\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::captured::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::shadowed::write"}
    )


def test_public_call_inventory_honors_nested_self_rebinding(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "class AuditContinuityVerifier:\n"
        "    async def outer(self, repository: AuditRepository, record):\n"
        "        self._repository = repository\n"
        "        async def captured():\n"
        "            await self._repository.write(record)\n"
        "        async def shadowed(self):\n"
        "            await self._repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::captured::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::shadowed::write"}
    )


def test_public_call_inventory_scopes_local_repository_class_aliases(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "def define_alias():\n"
        "    Repo = AuditRepository\n"
        "async def unrelated(repo: Repo, record):\n"
        "    await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::unrelated::write"}
    )


def test_public_call_inventory_honors_local_type_alias_closure_shadowing(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def outer(record):\n"
        "    Repo = AuditRepository\n"
        "    async def captured(repo: Repo):\n"
        "        await repo.write(record)\n"
        "    async def shadowed(Repo, repo: Repo):\n"
        "        await repo.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::captured::write"}
    )
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::shadowed::write"}
    )


def test_public_call_inventory_treats_loop_target_as_a_local_shadow(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository, records):\n"
        "    for repository in records:\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_rebound_repository_path(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "async def use(repository: AuditRepository):\n"
        "    repository = holder\n"
        "    await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::write"}
    )


def test_public_call_inventory_rejects_scoped_factory_shadowed_by_closure(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "factory = AuditRepository.scoped\n"
        "async def outer(factory, record):\n"
        "    async def inner():\n"
        "        repository = factory(db, scope)\n"
        "        await repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::inner::write"}
    )


def test_public_call_inventory_scopes_reviewed_edges_to_qualified_owner(tmp_path: Path) -> None:
    module = tmp_path / "src" / "zeroth" / "governance" / "audit" / "verifier.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class AuditContinuityVerifier:\n"
        "    async def verify_run(self):\n"
        "        await self._repository.list_by_run(run_id)\n"
        "class Unrelated:\n"
        "    async def verify_run(self):\n"
        "        await self._repository.list_by_run(run_id)\n",
        encoding="utf-8",
    )

    call = "src/zeroth/governance/audit/verifier.py::verify_run::list_by_run"
    assert _audit_repository_public_call_inventory(tmp_path) == frozenset({call})
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset({call})


def test_unreviewed_call_gate_reports_disallowed_typed_collaborator_operation(
    tmp_path: Path,
) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit.delivery_state import AuditRecordWriter\n"
        "async def use(writer: AuditRecordWriter):\n"
        "    await writer.list_by_run(run_id)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"apps/candidate.py::use::list_by_run"}
    )


def test_unreviewed_call_gate_reports_disallowed_reviewed_receiver_operation(
    tmp_path: Path,
) -> None:
    module = tmp_path / "src" / "zeroth" / "governance" / "audit" / "verifier.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "class AuditContinuityVerifier:\n"
        "    async def verify_run(self, record):\n"
        "        await self._repository.write(record)\n",
        encoding="utf-8",
    )

    assert _audit_repository_public_call_inventory(tmp_path) == frozenset()
    assert _unreviewed_audit_repository_public_calls(tmp_path) == frozenset(
        {"src/zeroth/governance/audit/verifier.py::verify_run::write"}
    )


def _canonical_audit_repository_name(name: tuple[str, ...]) -> tuple[str, ...]:
    if name == _AUDIT_REPOSITORY_IMPLEMENTATION_MODULE:
        return _AUDIT_REPOSITORY_MODULE
    if name == (*_AUDIT_REPOSITORY_IMPLEMENTATION_MODULE, "AuditRepository"):
        return _AUDIT_REPOSITORY_CLASS
    return name


def _resolved_audit_repository_name(
    node: ast.AST,
    repository_names: set[str],
    module_names: dict[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        if node.id in repository_names:
            return _AUDIT_REPOSITORY_CLASS
        return module_names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolved_audit_repository_name(node.value, repository_names, module_names)
        if base is not None:
            return _canonical_audit_repository_name((*base, node.attr))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        base = _resolved_audit_repository_name(node.args[0], repository_names, module_names)
        if base is not None:
            return _canonical_audit_repository_name((*base, node.args[1].value))
    return None


def _audit_repository_binding_inventory(root: Path) -> frozenset[str]:
    """Return the reviewed production AuditRepository construction inventory."""
    inventory: set[str] = set()
    for search_root in (
        root / "src",
        root / "release",
        root / "apps",
        root / "examples",
        root / "packaging" / "console" / "src",
    ):
        for path in search_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            repository_names: set[str] = set()
            module_names: dict[str, tuple[str, ...]] = {}
            relative_path = path.relative_to(root).as_posix()
            if relative_path == "src/zeroth/governance/audit/repository.py":
                repository_names.add("AuditRepository")
            for imported in ast.walk(tree):
                if isinstance(imported, ast.ImportFrom):
                    for alias in imported.names:
                        if (
                            imported.module
                            in {
                                "zeroth.governance.audit",
                                "zeroth.governance.audit.repository",
                            }
                            and alias.name == "AuditRepository"
                        ):
                            repository_names.add(alias.asname or alias.name)
                        elif imported.module in {
                            "zeroth.governance",
                            "zeroth.governance.audit",
                        } and alias.name in {"audit", "repository"}:
                            module_names[alias.asname or alias.name] = _AUDIT_REPOSITORY_MODULE
                elif isinstance(imported, ast.Import):
                    for alias in imported.names:
                        if alias.name in {
                            "zeroth.governance.audit",
                            "zeroth.governance.audit.repository",
                        }:
                            if alias.asname:
                                module_names[alias.asname] = _canonical_audit_repository_name(
                                    tuple(alias.name.split("."))
                                )
                            else:
                                module_names["zeroth"] = ("zeroth",)

            changed = True
            while changed:
                changed = False
                for assigned in ast.walk(tree):
                    if isinstance(assigned, ast.Assign) and len(assigned.targets) == 1:
                        target = assigned.targets[0]
                        value = assigned.value
                    elif isinstance(assigned, ast.AnnAssign):
                        target = assigned.target
                        value = assigned.value
                    else:
                        continue
                    if not isinstance(target, ast.Name) or value is None:
                        continue
                    binding = _resolved_audit_repository_name(value, repository_names, module_names)
                    if binding == _AUDIT_REPOSITORY_CLASS and target.id not in repository_names:
                        repository_names.add(target.id)
                        changed = True
                    elif binding is not None and target.id not in module_names:
                        module_names[target.id] = binding
                        changed = True

            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callable_identity = _resolved_audit_repository_name(
                    node.func, repository_names, module_names
                )
                direct_constructor = callable_identity == _AUDIT_REPOSITORY_CLASS
                if direct_constructor:
                    raise AssertionError(
                        f"production direct AuditRepository constructor at {path}:{node.lineno}"
                    )
                if callable_identity == (*_AUDIT_REPOSITORY_CLASS, "for_default_compatibility"):
                    raise AssertionError(
                        f"production default AuditRepository compatibility at {path}:{node.lineno}"
                    )
                if callable_identity != (*_AUDIT_REPOSITORY_CLASS, "scoped"):
                    continue
                if len(node.args) < 2 and not any(
                    keyword.arg == "scope_context" for keyword in node.keywords
                ):
                    raise AssertionError(
                        f"scope-free AuditRepository.scoped at {path}:{node.lineno}"
                    )
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
                inventory.add(f"{relative_path}::{owner_name}::scoped")
    return frozenset(inventory)


def test_production_audit_repository_has_one_explicit_scoped_constructor() -> None:
    """Keep the complete production construction surface owner-bound."""
    root = Path(__file__).resolve().parents[2]
    assert _audit_repository_binding_inventory(root) == frozenset(
        {"src/zeroth/service/bootstrap/factory.py::bootstrap_service::scoped"}
    )


def test_audit_repository_public_surface_is_exhaustive_and_scope_is_required() -> None:
    """A public operation can only exist on an instance whose owner was bound."""
    signature = python_inspect.signature(AuditRepository)
    assert signature.parameters["scope_context"].default is python_inspect.Parameter.empty
    assert {name for name in vars(AuditRepository) if not name.startswith("_")} == {
        "configure_capture",
        "crypto_erase",
        "crypto_erase_in_transaction",
        "for_default_compatibility",
        "get",
        "list",
        "list_by_deployment",
        "list_by_graph_version",
        "list_by_node",
        "list_by_run",
        "list_by_run_in_transaction",
        "list_by_thread",
        "list_erasable",
        "list_erasable_in_transaction",
        "scoped",
        "write",
        "write_many",
    }


@pytest.mark.parametrize(
    "source",
    [
        "import zeroth.governance.audit\nzeroth.governance.audit.AuditRepository(db, scope)\n",
        "from zeroth.governance import audit\naudit.AuditRepository(db, scope)\n",
        "import zeroth.governance.audit.repository as repository\nrepository.AuditRepository(db, scope)\n",
        "from zeroth.governance.audit import AuditRepository as Repo\nRepo(db, scope)\n",
        "from zeroth.governance.audit import AuditRepository\nRepo = AuditRepository\nRepo(db, scope)\n",
        "from zeroth.governance.audit import AuditRepository\nRepo: type = AuditRepository\nRepo(db, scope)\n",
        (
            "from zeroth.governance import audit\n"
            "Repo = getattr(audit, 'AuditRepository')\nRepo(db, scope)\n"
        ),
        ("from zeroth.governance import audit\ngetattr(audit, 'AuditRepository')(db, scope)\n"),
    ],
)
def test_audit_repository_inventory_rejects_qualified_and_aliased_direct_construction(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "apps" / "reference_app" / "entrypoint.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="production direct AuditRepository constructor"):
        _audit_repository_binding_inventory(tmp_path)


@pytest.mark.parametrize(
    "directory",
    ["src", "release", "apps", "examples", "packaging/console/src"],
)
def test_audit_repository_inventory_scans_every_shipped_python_root(
    tmp_path: Path, directory: str
) -> None:
    module = tmp_path / directory / "candidate.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\nAuditRepository(db, scope)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="production direct AuditRepository constructor"):
        _audit_repository_binding_inventory(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "class AuditRepository:\n    pass\nAuditRepository()\n",
        "from another_package import AuditRepository\nAuditRepository(db)\n",
        "import another_package as audit\naudit.AuditRepository(db)\n",
        "from zeroth.governance import audit\ngetattr(audit, 'UnrelatedRepository')(db)\n",
    ],
)
def test_audit_repository_inventory_ignores_unrelated_symbols(tmp_path: Path, source: str) -> None:
    module = tmp_path / "src" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    assert _audit_repository_binding_inventory(tmp_path) == frozenset()


@pytest.mark.parametrize(
    "source",
    [
        (
            "import zeroth.governance.audit\n"
            "def bind():\n"
            "    return zeroth.governance.audit.AuditRepository.scoped(db, scope)\n"
        ),
        (
            "from zeroth.governance import audit\n"
            "def bind():\n"
            "    return getattr(audit, 'AuditRepository').scoped(db, scope)\n"
        ),
        (
            "from zeroth.governance.audit.repository import AuditRepository as Repo\n"
            "def bind():\n"
            "    return Repo.scoped(db, scope_context=scope)\n"
        ),
    ],
)
def test_audit_repository_inventory_allows_only_explicit_scoped_factories(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "src" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    assert _audit_repository_binding_inventory(tmp_path) == frozenset(
        {"src/candidate.py::bind::scoped"}
    )


def test_audit_repository_inventory_rejects_default_compatibility_in_shipped_code(
    tmp_path: Path,
) -> None:
    module = tmp_path / "examples" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "AuditRepository.for_default_compatibility(db)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="production default AuditRepository compatibility"):
        _audit_repository_binding_inventory(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        (
            "from zeroth.governance.audit import AuditRepository\n"
            "factory = AuditRepository.scoped\nfactory(db)\n"
        ),
        (
            "from zeroth.governance.audit import AuditRepository\n"
            "factory: object = AuditRepository.scoped\nfactory(db)\n"
        ),
        (
            "from zeroth.governance.audit import AuditRepository\n"
            "factory = getattr(AuditRepository, 'scoped')\nfactory(db)\n"
        ),
        ("import zeroth.governance.audit as audit\ngetattr(audit.AuditRepository, 'scoped')(db)\n"),
    ],
)
def test_audit_repository_inventory_checks_scope_on_factory_alias_invocation(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "src" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="scope-free AuditRepository.scoped"):
        _audit_repository_binding_inventory(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        (
            "from zeroth.governance.audit import AuditRepository\n"
            "factory = AuditRepository.for_default_compatibility\nfactory(db)\n"
        ),
        (
            "from zeroth.governance.audit import AuditRepository\n"
            "factory: object = getattr(AuditRepository, 'for_default_compatibility')\n"
            "factory(db)\n"
        ),
        (
            "import zeroth.governance.audit as audit\n"
            "getattr(audit.AuditRepository, 'for_default_compatibility')(db)\n"
        ),
    ],
)
def test_audit_repository_inventory_rejects_compatibility_factory_alias_invocation(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "release" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="production default AuditRepository compatibility"):
        _audit_repository_binding_inventory(tmp_path)


def test_audit_repository_inventory_records_valid_scoped_factory_alias(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.governance.audit import AuditRepository\n"
        "factory = AuditRepository.scoped\n"
        "def bind():\n"
        "    return factory(db, scope)\n",
        encoding="utf-8",
    )

    assert _audit_repository_binding_inventory(tmp_path) == frozenset(
        {"apps/candidate.py::bind::scoped"}
    )


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
        "apps/vendor_dd/entrypoint.py::contract_registry_for_deployment::scoped",
        "apps/vendor_dd/seed.py::main::scoped",
        "src/zeroth/contracts/registry/registry.py::for_scope::scoped",
        "src/zeroth/service/bootstrap/factory.py::bootstrap_service::scoped",
        "src/zeroth/service/bootstrap/factory.py::build_runners_for_deployment::scoped",
        "src/zeroth/service/demo.py::seed_demo::default_compatibility",
    }
)

_CONTRACT_REGISTRY_CLASS = ("zeroth", "contracts", "registry", "ContractRegistry")
_CONTRACT_REGISTRY_MODULE = _CONTRACT_REGISTRY_CLASS[:-1]
_CONTRACT_REGISTRY_IMPLEMENTATION_MODULE = (*_CONTRACT_REGISTRY_MODULE, "registry")


def _canonical_contract_registry_name(name: tuple[str, ...]) -> tuple[str, ...]:
    if name == _CONTRACT_REGISTRY_IMPLEMENTATION_MODULE:
        return _CONTRACT_REGISTRY_MODULE
    if name == (*_CONTRACT_REGISTRY_IMPLEMENTATION_MODULE, "ContractRegistry"):
        return _CONTRACT_REGISTRY_CLASS
    return name


def _resolved_contract_registry_name(
    node: ast.AST,
    registry_names: set[str],
    module_names: dict[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Name):
        if node.id in registry_names:
            return _CONTRACT_REGISTRY_CLASS
        return module_names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolved_contract_registry_name(node.value, registry_names, module_names)
        if base is not None:
            return _canonical_contract_registry_name((*base, node.attr))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) == 2
        and _resolved_contract_registry_name(node.args[0], registry_names, module_names)
        == _CONTRACT_REGISTRY_MODULE
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "ContractRegistry"
    ):
        return _CONTRACT_REGISTRY_CLASS
    return None


def _assigned_contract_registry_binding(
    node: ast.AST,
    registry_names: set[str],
    module_names: dict[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    resolved = _resolved_contract_registry_name(node, registry_names, module_names)
    if resolved is not None:
        return resolved
    return None


def _contract_registry_binding_inventory(root: Path) -> frozenset[str]:
    inventory: set[str] = set()
    for search_root in (
        root / "src",
        root / "release",
        root / "apps",
        root / "examples",
        root / "packaging" / "console" / "src",
    ):
        for path in search_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            registry_names: set[str] = set()
            module_names: dict[str, tuple[str, ...]] = {}
            if path.relative_to(root).as_posix() == "src/zeroth/contracts/registry/registry.py":
                registry_names.add("ContractRegistry")
            for imported in ast.walk(tree):
                if isinstance(imported, ast.ImportFrom):
                    for alias in imported.names:
                        if (
                            imported.module
                            in {
                                "zeroth.contracts.registry",
                                "zeroth.contracts.registry.registry",
                            }
                            and alias.name == "ContractRegistry"
                        ):
                            registry_names.add(alias.asname or alias.name)
                        elif (
                            imported.module in {"zeroth.contracts", "zeroth.contracts.registry"}
                            and alias.name == "registry"
                        ):
                            module_names[alias.asname or alias.name] = _CONTRACT_REGISTRY_MODULE
                elif isinstance(imported, ast.Import):
                    for alias in imported.names:
                        if alias.name in {
                            "zeroth.contracts.registry",
                            "zeroth.contracts.registry.registry",
                        }:
                            if alias.asname:
                                module_names[alias.asname] = _CONTRACT_REGISTRY_MODULE
                            else:
                                module_names["zeroth"] = ("zeroth",)

            changed = True
            while changed:
                changed = False
                for assigned in ast.walk(tree):
                    if isinstance(assigned, ast.Assign) and len(assigned.targets) == 1:
                        target = assigned.targets[0]
                        value = assigned.value
                    elif isinstance(assigned, ast.AnnAssign):
                        target = assigned.target
                        value = assigned.value
                    else:
                        continue
                    if not isinstance(target, ast.Name) or value is None:
                        continue
                    binding = _assigned_contract_registry_binding(
                        value, registry_names, module_names
                    )
                    if binding == _CONTRACT_REGISTRY_CLASS and target.id not in registry_names:
                        registry_names.add(target.id)
                        changed = True
                    elif binding is not None and target.id not in module_names:
                        module_names[target.id] = binding
                        changed = True
            parents: dict[ast.AST, ast.AST] = {}
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                kind: str | None = None
                registry_receiver = (
                    isinstance(node.func, ast.Attribute)
                    and _resolved_contract_registry_name(
                        node.func.value, registry_names, module_names
                    )
                    == _CONTRACT_REGISTRY_CLASS
                )
                direct_constructor = (
                    _resolved_contract_registry_name(node.func, registry_names, module_names)
                    == _CONTRACT_REGISTRY_CLASS
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


def test_contract_registry_inventory_scans_reference_apps(tmp_path: Path) -> None:
    module = tmp_path / "apps" / "reference_app" / "entrypoint.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from zeroth.contracts.registry import ContractRegistry\nContractRegistry(db)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="production legacy ContractRegistry constructor"):
        _contract_registry_binding_inventory(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "import zeroth.contracts.registry\nzeroth.contracts.registry.ContractRegistry(db)\n",
        "from zeroth.contracts import registry\nregistry.ContractRegistry(db)\n",
        ("from zeroth.contracts import registry\ngetattr(registry, 'ContractRegistry')(db)\n"),
        ("from zeroth.contracts.registry.registry import ContractRegistry\nContractRegistry(db)\n"),
        ("import zeroth.contracts.registry.registry as registry\nregistry.ContractRegistry(db)\n"),
        (
            "from zeroth.contracts.registry import ContractRegistry\n"
            "R: type = ContractRegistry\n"
            "R(db)\n"
        ),
        (
            "import zeroth.contracts.registry as registry_module\n"
            "alias = registry_module\n"
            "alias.ContractRegistry(db)\n"
        ),
        (
            "from zeroth.contracts import registry\n"
            "R = getattr(registry, 'ContractRegistry')\n"
            "R(db)\n"
        ),
    ],
)
def test_contract_registry_binding_inventory_rejects_qualified_and_aliased_construction(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "src" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="production legacy ContractRegistry constructor"):
        _contract_registry_binding_inventory(tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "class ContractRegistry:\n    pass\nContractRegistry()\n",
        "from another_package import ContractRegistry\nContractRegistry(db)\n",
        "import another_package as registry\nregistry.ContractRegistry(db)\n",
        "class Registry:\n    ContractRegistry = object\nregistry = Registry()\nregistry.ContractRegistry()\n",
        ("import another_package as registry\ngetattr(registry, 'ContractRegistry')(db)\n"),
        ("from zeroth.contracts import registry\ngetattr(registry, 'UnrelatedClass')(db)\n"),
    ],
)
def test_contract_registry_binding_inventory_ignores_unrelated_symbols(
    tmp_path: Path, source: str
) -> None:
    module = tmp_path / "src" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    assert _contract_registry_binding_inventory(tmp_path) == frozenset()


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (
            "import zeroth.contracts.registry\n"
            "def bind():\n"
            "    return zeroth.contracts.registry.ContractRegistry.scoped(db, scope)\n",
            "scoped",
        ),
        (
            "from zeroth.contracts import registry\n"
            "def bind():\n"
            "    return registry.ContractRegistry.for_default_compatibility(db)\n",
            "default_compatibility",
        ),
        (
            "import zeroth.contracts.registry.registry as registry\n"
            "def bind():\n"
            "    return registry.ContractRegistry.scoped(db, scope)\n",
            "scoped",
        ),
        (
            "from zeroth.contracts import registry\n"
            "def bind():\n"
            "    return getattr(registry, 'ContractRegistry').scoped(db, scope)\n",
            "scoped",
        ),
        (
            "from zeroth.contracts import registry\n"
            "def bind():\n"
            "    return getattr(registry, 'ContractRegistry').for_default_compatibility(db)\n",
            "default_compatibility",
        ),
    ],
)
def test_contract_registry_binding_inventory_allows_explicit_qualified_factories(
    tmp_path: Path, source: str, kind: str
) -> None:
    module = tmp_path / "src" / "candidate.py"
    module.parent.mkdir(parents=True)
    module.write_text(source, encoding="utf-8")

    assert _contract_registry_binding_inventory(tmp_path) == frozenset(
        {f"src/candidate.py::bind::{kind}"}
    )


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
