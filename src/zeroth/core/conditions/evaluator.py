"""Safe, deterministic condition evaluation engine.

This module takes a condition expression (like "payload.status == 'done'") and
evaluates it against runtime data without using Python's built-in eval().
Instead, it parses the expression into an AST and walks it node by node,
only allowing a safe subset of operations. This prevents code injection.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from zeroth.core.conditions.errors import ConditionEvaluationError
from zeroth.core.conditions.models import ConditionContext
from zeroth.core.graph.models import Condition as GraphCondition
from zeroth.core.runs.models import RunConditionResult

_SAFE_BUILTINS: frozenset[str] = frozenset(
    {
        "len",
        "str",
        "int",
        "float",
        "bool",
        "abs",
        "min",
        "max",
        "round",
        "sorted",
    }
)

_SAFE_BUILTIN_MAP: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sorted": sorted,
}


def _is_truthy(value: Any) -> bool:
    """Check whether a value is truthy using standard Python truthiness rules."""
    return bool(value)


def _path_lookup(namespace: Mapping[str, Any], path: str) -> Any:
    """Walk a dot-separated path (like "payload.user.name") through nested dicts/objects.

    Returns None if any segment along the path is missing.
    """
    current: Any = namespace
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
            continue
        if hasattr(current, part):
            current = getattr(current, part)
            continue
        return None
    return current


class _SafeEvaluator:
    """Evaluates a Python expression safely by walking its AST.

    Unlike Python's built-in eval(), this only allows a limited set of
    operations (comparisons, boolean logic, arithmetic, lookups). It will
    raise ConditionEvaluationError if the expression tries to do anything
    that is not on the allow-list.
    """

    def __init__(self, namespace: Mapping[str, Any]):
        self._namespace = namespace

    def evaluate(self, expression: str) -> Any:
        """Parse and evaluate the expression, returning its computed value."""
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:  # pragma: no cover - guarded by tests
            raise ConditionEvaluationError(f"invalid condition expression: {expression!r}") from exc
        return self._visit(tree.body)

    def _visit(self, node: ast.AST) -> Any:
        """Recursively evaluate a single AST node and return its value."""
        match node:
            case ast.Constant():
                return node.value
            case ast.Name():
                if node.id in _SAFE_BUILTIN_MAP:
                    return _SAFE_BUILTIN_MAP[node.id]
                return self._namespace.get(node.id)
            case ast.Attribute():
                value = self._visit(node.value)
                if isinstance(value, Mapping):
                    return value.get(node.attr)
                return getattr(value, node.attr, None)
            case ast.Subscript():
                value = self._visit(node.value)
                index = self._visit(node.slice)
                try:
                    return value[index]
                except Exception:
                    return None
            case ast.List():
                return [self._visit(element) for element in node.elts]
            case ast.Tuple():
                return tuple(self._visit(element) for element in node.elts)
            case ast.Dict():
                return {
                    self._visit(key): self._visit(value)
                    for key, value in zip(node.keys, node.values, strict=True)
                }
            case ast.Set():
                return {self._visit(element) for element in node.elts}
            case ast.BoolOp():
                values = [self._visit(value) for value in node.values]
                if isinstance(node.op, ast.And):
                    return all(_is_truthy(value) for value in values)
                if isinstance(node.op, ast.Or):
                    return any(_is_truthy(value) for value in values)
                raise ConditionEvaluationError(f"unsupported boolean operator: {ast.dump(node.op)}")
            case ast.UnaryOp():
                value = self._visit(node.operand)
                if isinstance(node.op, ast.Not):
                    return not _is_truthy(value)
                if isinstance(node.op, ast.UAdd):
                    return +value
                if isinstance(node.op, ast.USub):
                    return -value
                raise ConditionEvaluationError(f"unsupported unary operator: {ast.dump(node.op)}")
            case ast.BinOp():
                left = self._visit(node.left)
                right = self._visit(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.Mod):
                    return left % right
                raise ConditionEvaluationError(f"unsupported binary operator: {ast.dump(node.op)}")
            case ast.Compare():
                left = self._visit(node.left)
                for operator, comparator in zip(node.ops, node.comparators, strict=True):
                    right = self._visit(comparator)
                    if isinstance(operator, ast.Eq):
                        matched = left == right
                    elif isinstance(operator, ast.NotEq):
                        matched = left != right
                    elif isinstance(operator, ast.Lt):
                        matched = left < right
                    elif isinstance(operator, ast.LtE):
                        matched = left <= right
                    elif isinstance(operator, ast.Gt):
                        matched = left > right
                    elif isinstance(operator, ast.GtE):
                        matched = left >= right
                    elif isinstance(operator, ast.In):
                        matched = left in right
                    elif isinstance(operator, ast.NotIn):
                        matched = left not in right
                    elif isinstance(operator, ast.Is):
                        matched = left is right
                    elif isinstance(operator, ast.IsNot):
                        matched = left is not right
                    else:  # pragma: no cover - defensive
                        raise ConditionEvaluationError(
                            f"unsupported comparison operator: {ast.dump(operator)}"
                        )
                    if not matched:
                        return False
                    left = right
                return True
            case ast.IfExp():
                test_value = self._visit(node.test)
                if _is_truthy(test_value):
                    return self._visit(node.body)
                return self._visit(node.orelse)
            case ast.Call():
                func = self._visit(node.func)
                if func not in _SAFE_BUILTIN_MAP.values():
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    else:
                        func_name = ast.dump(node.func)
                    raise ConditionEvaluationError(f"'{func_name}' is not an allowed safe builtin")
                args = [self._visit(arg) for arg in node.args]
                kwargs = {kw.arg: self._visit(kw.value) for kw in node.keywords}
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    raise ConditionEvaluationError(f"safe builtin call failed: {exc}") from exc
            case _:
                raise ConditionEvaluationError(
                    f"unsupported expression node: {type(node).__name__}"
                )


class ConditionEvaluator:
    """Evaluates graph conditions against runtime context data.

    This is the main entry point for condition evaluation. Give it a condition
    (from a graph edge) and a context (the current runtime data), and it will
    tell you whether the condition matched and what value it produced.
    """

    def evaluate(
        self,
        condition: GraphCondition,
        context: ConditionContext | Mapping[str, Any],
        *,
        condition_id: str | None = None,
        edge_id: str | None = None,
        source_node_id: str | None = None,
        target_node_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunConditionResult:
        """Evaluate a single condition and return a result with match info and details.

        The returned RunConditionResult tells you whether the condition matched,
        which edge was selected, and includes debugging details like the expression
        and operand references.
        """
        runtime_context = self._as_context(context)
        truth, value = self._evaluate_condition(condition, runtime_context)
        details = {
            "expression": condition.expression,
            "branch_rule": condition.branch_rule,
            "operand_refs": list(condition.operand_refs),
            "value": value,
            "graph_context": dict(metadata or {}),
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "edge_id": edge_id,
        }
        return RunConditionResult(
            condition_id=condition_id or edge_id or condition.expression,
            selected_edge_id=edge_id if truth else None,
            matched=truth,
            details=details,
        )

    def evaluate_operand_ref(self, ref: str, context: ConditionContext | Mapping[str, Any]) -> Any:
        """Look up a single operand reference (like "payload.score") in the context."""
        runtime_context = self._as_context(context)
        namespace = runtime_context.namespace()
        if ref in namespace:
            return namespace[ref]
        return _path_lookup(namespace, ref)

    def _evaluate_condition(
        self,
        condition: GraphCondition,
        context: ConditionContext,
    ) -> tuple[bool, Any]:
        """Run the condition's expression or branch rule and return (matched, value).

        If the condition uses an "all" or "any" branch rule with operand refs,
        those are resolved first. Otherwise the expression is evaluated using
        the safe evaluator.
        """
        namespace = context.namespace()
        if condition.branch_rule == "all" and condition.operand_refs:
            resolved = [self.evaluate_operand_ref(ref, context) for ref in condition.operand_refs]
            value = all(_is_truthy(item) for item in resolved)
            return value, resolved
        if condition.branch_rule == "any" and condition.operand_refs:
            resolved = [self.evaluate_operand_ref(ref, context) for ref in condition.operand_refs]
            value = any(_is_truthy(item) for item in resolved)
            return value, resolved

        evaluator = _SafeEvaluator(namespace)
        value = evaluator.evaluate(condition.expression)
        return _is_truthy(value), value

    def _as_context(
        self,
        context: ConditionContext | Mapping[str, Any],
    ) -> ConditionContext:
        """Ensure the context is a ConditionContext, converting from a dict if needed."""
        if isinstance(context, ConditionContext):
            return context
        return ConditionContext.model_validate(dict(context))
