"""Characterization tests for Zeroth's supported backend library surface.

The legacy snapshot is a capability contract, not a promise that old import
locations remain forever.  The canonical snapshot maps each protected legacy
capability to its current import location and is updated alongside the backend
import migration guide when ownership changes.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_settings import BaseSettings

FIXTURES = Path(__file__).parents[1] / "contracts" / "fixtures"
REPO_ROOT = Path(__file__).parents[2]
CANONICAL_PACKAGES = (
    "zeroth.runtime",
    "zeroth.runtime.orchestration",
    "zeroth.runtime.agents",
    "zeroth.runtime.context",
    "zeroth.runtime.parallel",
    "zeroth.runtime.runs",
    "zeroth.runtime.subgraphs",
    "zeroth.governance",
    "zeroth.governance.approvals",
    "zeroth.governance.audit",
    "zeroth.governance.identity",
    "zeroth.governance.policy",
    "zeroth.governance.guardrails",
    "zeroth.governance.retention",
    "zeroth.governance.langgraph_gateway",
    "zeroth.platform",
    "zeroth.platform.artifacts",
    "zeroth.platform.config",
    "zeroth.platform.dispatch",
    "zeroth.platform.observability",
    "zeroth.platform.persistence",
    "zeroth.platform.primitives",
    "zeroth.platform.secrets",
    "zeroth.platform.signing",
    "zeroth.platform.storage",
    "zeroth.contracts",
    "zeroth.contracts.conditions",
    "zeroth.contracts.governed",
    "zeroth.contracts.graph",
    "zeroth.contracts.langgraph_gateway",
    "zeroth.contracts.registry",
    "zeroth.contracts.mappings",
    "zeroth.contracts.templates",
    "zeroth.service",
    "zeroth.service.api",
    "zeroth.service.bootstrap",
    "zeroth.service.langgraph_gateway",
    "zeroth.service.deployments",
    "zeroth.service.webhooks",
    "zeroth.econ",
    "zeroth.econ.analytics",
    "zeroth.econ.instrumentation",
    "zeroth.econ.plane",
    "zeroth.optimization",
    "zeroth.integrations",
    "zeroth.integrations.execution",
    "zeroth.integrations.http",
    "zeroth.integrations.memory",
    "zeroth.integrations.persistence",
    "zeroth.integrations.rag",
    "zeroth.integrations.sandbox",
    "zeroth.eval",
)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("module_name", CANONICAL_PACKAGES)
def test_every_canonical_package_imports(module_name: str) -> None:
    """The authoritative backend package tree must remain importable."""
    importlib.import_module(module_name)


def test_lazy_orchestration_surface_retains_static_type_bindings() -> None:
    """Lazy runtime exports remain concrete names to static analyzers."""
    path = REPO_ROOT / "src/zeroth/runtime/orchestration/__init__.py"
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
    assert {
        "GraphDriver",
        "NodeDispatcher",
        "RuntimePolicyGate",
        "TokenSnapshotStore",
        "TokenSnapshotConcurrencyError",
        "TokenSnapshotCorruptionError",
        "TokenSnapshotTransitionError",
        "TokenSnapshotWriteDisabledError",
    } <= imported


def _import_symbol(entry: dict[str, Any]) -> object:
    module = importlib.import_module(entry["module"])
    try:
        return getattr(module, entry["name"])
    except AttributeError:
        # Package __all__ may publish a lazily imported submodule.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"\s*on_event is deprecated.*",
                category=DeprecationWarning,
            )
            return importlib.import_module(f"{entry['module']}.{entry['name']}")


# A capability's identity is its shape, not the import path of the types in it.
#
# ``inspect.signature`` renders each annotation using that type's *defining*
# module, so relocating a class rewrites the signature of every symbol that
# mentions it. Because ``backend_surface_legacy.json`` is immutable, comparing
# raw strings would make the protected surface forbid exactly the moves this
# refactor exists to perform — and it contradicts what the migration guide says
# the legacy fixture is for: identifying capabilities "independently of their
# future import locations".
#
# Normalizing collapses ``zeroth.<anything>.SomeType`` to ``SomeType`` on BOTH
# sides at comparison time. The fixtures themselves are never rewritten, so the
# immutability rule holds literally.
#
# The cost, accepted deliberately: two same-named classes in different packages
# no longer compare as different. Parameter names, order, defaults, and the bare
# type names all remain pinned, and tests/architecture/test_backend_dependencies
# independently constrains which package may supply a symbol.
_ZEROTH_QUALIFIER = re.compile(r"zeroth\.[\w.]*?\.([A-Z]\w*)")


def _comparable(signature: str | None) -> str | None:
    """Return a signature with Zeroth's own module qualifiers removed."""
    if signature is None:
        return None
    comparable = _ZEROTH_QUALIFIER.sub(r"\1", signature)
    # ``annotated_types`` constraints compare 0 and 0.0 as equal, and Pydantic
    # may reuse either cached repr depending on import order. Normalize only
    # integral literals inside constraint reprs; defaults stay fully pinned.
    return re.sub(r"(?<=[(,])(ge|gt|le|lt)=(-?\d+)\.0(?=[,)])", r"\1=\2", comparable)


def _signature(value: object) -> str | None:
    if not callable(value):
        return None
    try:
        signature = str(inspect.signature(value))
        return re.sub(r" at 0x[0-9a-fA-F]+", " at 0x<address>", signature)
    except (TypeError, ValueError):
        # Some Python exception classes inherit an opaque built-in constructor.
        # Calling inspect.signature is still part of the smoke test; the marker
        # makes that interpreter-level fact explicit in the protected contract.
        return "<not-inspectable>"


# A settings class inherits its constructor's leading parameters from
# ``pydantic_settings.BaseSettings``, and those belong to that library, not to
# Zeroth.
#
# ``pyproject.toml`` declares ``pydantic-settings>=2.13`` with no upper bound, so
# ``uv sync`` resolves the lock (2.13.1) while a fresh ``pip install`` of the
# wheel resolves the newest release. Measured on nightly 31469899049: the wheel
# venv installed 2.15.0, which adds ``_cli_show_env_vars`` to
# ``BaseSettings.__init__``, and the package gate reported the protected surface
# of ``ZerothSettings`` as changed. Nothing about Zeroth had changed. The gate
# named a Zeroth capability and meant an upstream library's private CLI keyword.
#
# Pinning an upper bound would make the gate green by constraining every consumer
# to the version this repository happens to have locked, over a parameter no
# consumer can pass meaningfully. Instead the comparison drops exactly the
# parameters the installed base class contributes -- derived from that class, not
# listed here, so it tracks the library rather than a snapshot of it -- and
# ``test_the_upstream_exclusion_never_reaches_a_zeroth_owned_field`` refuses any
# derivation that would reach Zeroth's own fields.
def _upstream_owned_parameters(value: object) -> frozenset[str]:
    """Constructor parameter names ``value`` inherits from ``BaseSettings``."""
    if not (isinstance(value, type) and issubclass(value, BaseSettings)):
        return frozenset()
    inherited = set(inspect.signature(BaseSettings).parameters)
    return frozenset(inherited - set(value.model_fields))


def _bracket_depths(text: str) -> Iterator[tuple[int, str, int]]:
    """Each character with its bracket depth; anything inside quotes reports ``-1``.

    One state machine, so the two questions asked of it below -- where does the
    parameter list end, and which commas separate parameters -- cannot disagree
    about which brackets and quotes are real.
    """
    depth = 0
    quote = ""
    for position, character in enumerate(text):
        if quote:
            quote = "" if character == quote else quote
            yield position, character, -1
            continue
        if character in "\"'":
            quote = character
            yield position, character, -1
            continue
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        yield position, character, depth


def split_parameters(signature: str) -> tuple[list[str], str]:
    """A signature's parameter texts and everything after them.

    Splitting on ``", "`` is wrong here: annotations carry commas of their own --
    ``"bool | Literal['dual', 'toggle'] | None"`` and
    ``'Mapping[str, str | list[str]] | None'`` both appear in the real signature --
    so the split tracks bracket depth and quoting.

    Inside the outer parentheses the depth is 1, so a separating comma is a comma
    at depth 1 and the closing parenthesis is the ``)`` that returns to 0.
    """
    if not signature.startswith("("):
        return [], signature
    parameters: list[str] = []
    current = ""
    for position, character, depth in _bracket_depths(signature):
        if depth == 0 and character == ")":
            parameters.append(current)
            return [item.strip() for item in parameters if item.strip()], signature[position:]
        if depth == 1 and character == ",":
            parameters.append(current)
            current = ""
            continue
        if position:
            current += character
    return [item.strip() for item in parameters if item.strip()], ""


def parameter_name(parameter: str) -> str:
    """The bare name of one rendered parameter, without ``*``, annotation or default."""
    name = parameter.lstrip("*").strip()
    for separator in (":", "="):
        name = name.split(separator, 1)[0]
    return name.strip()


def without_parameters(signature: str, names: frozenset[str]) -> str:
    """``signature`` with every parameter in ``names`` removed."""
    if not names:
        return signature
    parameters, suffix = split_parameters(signature)
    kept = [item for item in parameters if parameter_name(item) not in names]
    return "(" + ", ".join(kept) + suffix


def _pinned(signature: str | None, upstream: frozenset[str]) -> str | None:
    """The part of a signature Zeroth owns, in comparable form."""
    if signature is None:
        return None
    return _comparable(without_parameters(signature, upstream))


def test_comparable_signature_drops_the_defining_module_of_zeroth_types() -> None:
    """Capability identity must not depend on where a type happens to live.

    ``inspect.signature`` renders an annotation using the defining module of
    each referenced type, so relocating a class rewrites the signature of every
    symbol that mentions it. Comparing normalized forms is what lets a protected
    capability keep its identity across a move.
    """
    relocated = "(*, who: zeroth.governance.identity.models.ActorIdentity) -> None"
    original = "(*, who: zeroth.core.identity.models.ActorIdentity) -> None"

    assert _comparable(relocated) == _comparable(original)
    assert _comparable(original) == "(*, who: ActorIdentity) -> None"


def test_the_upstream_exclusion_never_reaches_a_zeroth_owned_field() -> None:
    """The derivation may drop what the library owns and nothing else.

    ``ZerothSettings`` declares 22 fields of its own and inherits 28 constructor
    parameters from ``BaseSettings``. If those sets ever overlapped -- a Zeroth
    field named like an upstream keyword, or a future ``BaseSettings`` that
    promotes one -- the exclusion would silently stop pinning a real capability,
    which is the loophole this normalization could otherwise become.
    """
    from zeroth.platform.config import ZerothSettings

    upstream = _upstream_owned_parameters(ZerothSettings)

    assert upstream, "nothing excluded -- the derivation is not finding the base class"
    assert upstream.isdisjoint(set(ZerothSettings.model_fields)), sorted(
        upstream & set(ZerothSettings.model_fields)
    )
    assert _upstream_owned_parameters(BaseModel) == frozenset()


def test_a_zeroth_owned_field_change_is_still_reported() -> None:
    """The pin still pins. Fed the change it exists to catch, on the real class.

    Renaming one of Zeroth's own settings groups must fail the comparison even
    though the upstream parameters are being dropped from both sides.
    """
    from zeroth.platform.config import ZerothSettings

    upstream = _upstream_owned_parameters(ZerothSettings)
    actual = _signature(ZerothSettings)
    assert actual is not None
    assert actual.count(", retention: ") == 1, "the field this mutates is no longer unique"
    renamed = actual.replace(", retention: ", ", retention_v2: ")
    assert _pinned(actual, upstream) != _pinned(renamed, upstream)


def test_an_upstream_parameter_appearing_is_tolerated() -> None:
    """The exact divergence measured in the wheel venv, reproduced as a fixture.

    ``pydantic-settings`` 2.15.0 adds ``_cli_show_env_vars`` to
    ``BaseSettings.__init__``. Both sides drop the parameters the *installed*
    base class owns, so the side that has it and the side that does not compare
    equal -- and the two Zeroth fields around it stay pinned.
    """
    upstream = frozenset({"_cli_prefix", "_cli_show_env_vars"})
    without = (
        "(_cli_prefix: 'str | None' = None, *, database: DatabaseSettings = <factory>) -> None"
    )
    with_added = (
        "(_cli_show_env_vars: 'bool | None' = None, _cli_prefix: 'str | None' = None, "
        "*, database: DatabaseSettings = <factory>) -> None"
    )

    assert _pinned(without, upstream) == _pinned(with_added, upstream)
    assert _pinned(without, frozenset()) != _pinned(with_added, frozenset())
    assert "database: DatabaseSettings" in str(_pinned(with_added, upstream))


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        pytest.param(
            "(a: int = 1, b: str = 'x') -> None",
            ["a: int = 1", "b: str = 'x'"],
            id="plain_parameters",
        ),
        pytest.param(
            "(mode: \"bool | Literal['dual', 'toggle'] | None\" = None, b: int = 2) -> None",
            ["mode: \"bool | Literal['dual', 'toggle'] | None\" = None", "b: int = 2"],
            id="a_comma_inside_a_quoted_annotation",
        ),
        pytest.param(
            "(shortcuts: 'Mapping[str, str | list[str]] | None' = None) -> None",
            ["shortcuts: 'Mapping[str, str | list[str]] | None' = None"],
            id="a_comma_inside_brackets",
        ),
        pytest.param(
            "(*, a: int, **rest: Any) -> None",
            ["*", "a: int", "**rest: Any"],
            id="markers_and_var_keyword",
        ),
        pytest.param("() -> None", [], id="no_parameters"),
    ],
)
def test_the_parameter_splitter_survives_the_shapes_this_signature_takes(
    signature: str, expected: list[str]
) -> None:
    """A naive ``", "`` split mangles three of these five, and would drop real fields."""
    assert split_parameters(signature)[0] == expected


def test_the_parameter_splitter_keeps_the_return_annotation() -> None:
    """Dropping the suffix would make two different return types compare equal."""
    assert split_parameters("(a: int) -> Run")[1] == ") -> Run"
    assert without_parameters("(a: int, b: str) -> Run", frozenset({"a"})) == "(b: str) -> Run"


def test_comparable_signature_preserves_non_zeroth_qualifiers() -> None:
    """Only Zeroth's own module paths move; everything else stays pinned."""
    signature = "(*, when: datetime.datetime, what: typing.Any) -> None"

    assert _comparable(signature) == signature


def test_comparable_signature_still_separates_different_types() -> None:
    """Normalization drops the path, not the type name.

    This is the residual cost of the amendment: two same-named classes in
    different packages become indistinguishable here. The architecture
    dependency test is what independently constrains which package a symbol may
    come from.
    """
    assert _comparable("(x: zeroth.core.runs.models.Run) -> None") != _comparable(
        "(x: zeroth.core.runs.models.Thread) -> None"
    )


def test_comparable_signature_normalizes_integral_constraint_repr_only() -> None:
    assert _comparable("(*, x: Annotated[float, Ge(ge=0.0), Le(le=1.0)] = 0.0)") == (
        "(*, x: Annotated[float, Ge(ge=0), Le(le=1)] = 0.0)"
    )


def test_immutable_legacy_capabilities_remain_available_with_original_signatures() -> None:
    """Legacy capabilities may move, but they cannot silently disappear or change."""
    legacy = _load("backend_surface_legacy.json")
    canonical = _load("backend_surface_canonical.json")

    assert legacy["immutable"] is True
    assert canonical["evolving"] is True

    current_by_legacy_id: dict[str, dict[str, Any]] = {}
    for entry in canonical["symbols"]:
        for legacy_id in entry["legacy_ids"]:
            assert legacy_id not in current_by_legacy_id, f"duplicate legacy mapping: {legacy_id}"
            current_by_legacy_id[legacy_id] = entry

    missing = [
        capability["id"]
        for capability in legacy["capabilities"]
        if capability["id"] not in current_by_legacy_id
    ]
    assert not missing, f"legacy capabilities missing canonical replacements: {missing}"

    mismatches = []
    for capability in legacy["capabilities"]:
        current = current_by_legacy_id[capability["id"]]
        if _comparable(current["signature"]) != _comparable(capability["signature"]):
            mismatches.append(
                {
                    "capability": capability["id"],
                    "expected": capability["signature"],
                    "canonical": current["signature"],
                }
            )
    assert not mismatches, f"legacy signature changes: {mismatches}"


# A capability may keep its identity without staying importable. ZER-25 demotes
# the quickstart tutorial helper to repository content: it ships in ``examples/``
# and no longer in the wheel, so it has a ``repository_path`` instead of a
# ``module``. Its signature stays pinned -- it is loaded from its file below --
# so the protected capability is still verified, just not as an import.
_CANONICAL_SYMBOLS = _load("backend_surface_canonical.json")["symbols"]
_IMPORTABLE_SYMBOLS = [entry for entry in _CANONICAL_SYMBOLS if "module" in entry]
_REPOSITORY_SYMBOLS = [entry for entry in _CANONICAL_SYMBOLS if "repository_path" in entry]


@pytest.mark.parametrize(
    "entry",
    _IMPORTABLE_SYMBOLS,
    ids=lambda entry: f"{entry['module']}:{entry['name']}",
)
def test_every_canonical_symbol_imports_and_matches_its_signature(entry: dict[str, Any]) -> None:
    """The evolving canonical fixture is executable import documentation."""
    value = _import_symbol(entry)
    upstream = _upstream_owned_parameters(value)
    assert _pinned(_signature(value), upstream) == _pinned(entry["signature"], upstream)


def test_every_canonical_entry_is_either_importable_or_a_repository_file() -> None:
    """No entry may claim both homes, or neither -- that would skip it silently."""
    for entry in _CANONICAL_SYMBOLS:
        homes = {"module", "repository_path"} & set(entry)
        assert len(homes) == 1, f"{entry['name']}: expected exactly one home, got {sorted(homes)}"


@pytest.mark.parametrize(
    "entry",
    _REPOSITORY_SYMBOLS,
    ids=lambda entry: f"{entry['repository_path']}:{entry['name']}",
)
def test_every_repository_symbol_loads_from_its_file_and_matches_its_signature(
    entry: dict[str, Any],
) -> None:
    """A demoted capability is still pinned; it is just loaded from disk."""
    path = REPO_ROOT / entry["repository_path"]
    assert path.exists(), f"{entry['repository_path']} does not exist"

    spec = importlib.util.spec_from_file_location(f"_surface_{entry['name']}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    value = getattr(module, entry["name"])
    assert _comparable(_signature(value)) == _comparable(entry["signature"])


def test_surface_inventory_records_all_required_evidence_classes() -> None:
    """Guard against rebuilding the inventory from only package exports."""
    legacy = _load("backend_surface_legacy.json")
    evidence_classes = {
        evidence.split(":", 1)[0]
        for capability in legacy["capabilities"]
        for evidence in capability["evidence"]
    }
    assert {
        "__all__",
        "docs",
        "entry_point",
        "examples",
        "optional_integration",
        "package_export",
        "schema_model",
    } <= evidence_classes


def _discover_schema_models() -> set[str]:
    """Discover models in the same schema-bearing modules covered by the inventory."""
    source_root = REPO_ROOT / "src"
    zeroth_root = source_root / "zeroth"
    schema_paths = {
        path
        for path in zeroth_root.rglob("*.py")
        if path.name in {"models.py", "schemas.py"}
        or (
            # Schema-bearing service modules in both the legacy layout
            # (zeroth/core/service/*) and the canonical one, where the route
            # modules live in zeroth/service/api/ and the app composition
            # directly in zeroth/service/. The legacy paths are re-export
            # shims defining nothing, so scanning both cannot double-count.
            (
                path.parent.name == "service"
                or (path.parent.name == "api" and path.parent.parent.name == "service")
            )
            and (
                path.name.endswith("_api.py")
                or path.name in {"app.py", "health.py", "studio_schemas.py"}
            )
        )
    }
    discovered: set[str] = set()
    for path in schema_paths:
        module_name = ".".join(path.relative_to(source_root).with_suffix("").parts)
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isclass(value)
                and value.__module__ == module_name
                and issubclass(value, BaseModel)
            ):
                discovered.add(f"{module_name}:{name}")
    return discovered


def test_every_discovered_schema_model_is_in_legacy_and_canonical_surfaces() -> None:
    """Reverse coverage prevents schema modules from being silently omitted.

    Additive models created after the legacy snapshot have no historical import
    identity to preserve. They must still be pinned in the evolving canonical
    surface, with an explicitly empty legacy mapping; inventing a legacy ID
    would weaken rather than preserve the immutable contract.
    """
    legacy = _load("backend_surface_legacy.json")
    canonical = _load("backend_surface_canonical.json")
    legacy_ids = {entry["id"] for entry in legacy["capabilities"]}
    # Only importable entries can be reverse-matched against discovered schema
    # models: a repository-only capability has no module path to key on, and by
    # construction is never a schema model discovered under ``src``.
    canonical_by_current_id = {
        f"{entry['module']}:{entry['name']}": entry
        for entry in canonical["symbols"]
        if "module" in entry
    }
    discovered = _discover_schema_models()

    missing_canonical = sorted(discovered - canonical_by_current_id.keys())
    assert not missing_canonical, f"schema models missing canonical entries: {missing_canonical}"

    missing_legacy = sorted(
        current_id
        for current_id in discovered
        if canonical_by_current_id[current_id]["legacy_ids"]
        if not (set(canonical_by_current_id[current_id]["legacy_ids"]) & legacy_ids)
    )
    assert not missing_legacy, f"schema models missing protected legacy IDs: {missing_legacy}"

    invented_legacy_ids = sorted(
        current_id
        for current_id in discovered
        if any(
            legacy_id not in legacy_ids
            for legacy_id in canonical_by_current_id[current_id]["legacy_ids"]
        )
    )
    assert not invented_legacy_ids, (
        f"new schemas must not claim invented legacy identities: {invented_legacy_ids}"
    )
