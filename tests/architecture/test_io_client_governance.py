"""Transport clients are constructed by a governed factory, not at call sites.

The mechanism this guard exists for is *not* "libraries default to blocking
forever" -- that is true of aioredis and chromadb and false of psycopg 3.3.3,
which is why one audit finding was refuted on it. The mechanism that survives
both is: **clients are constructed ad-hoc at call sites, so timeout, retry and
lifecycle policy is per-site and unenforceable.** A construction the factory does
not own is a policy nobody reviewed.

The record below is an *exact set*, compared with ``==`` in both directions. A
subset check would let new violations in; a superset check would let an entry
outlive the code it describes. Exact means the record can only shrink, which is
the whole point of a ratchet -- and it is the same shape
``tests/architecture/test_persistence_scope_inventory.py`` already uses.

One gap this guard cannot close on its own, stated rather than papered over: the
``asyncio.wait_for(..., timeout=None)`` probe matches a *literal* ``None``. The
real unbounded-sandbox path (ZER-48 / A07-2) passed a **variable** whose value
was ``None``, which no source-level scan can see. That is closed by the model
default refusing to produce ``None`` at all, tested separately -- not here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/zeroth"

#: Modules allowed to construct transport clients directly: they *are* the
#: governed layer, so banning construction inside them would ban the fix.
GOVERNED_MODULES = frozenset(
    {
        "integrations/http/client.py",
        "integrations/memory/factory.py",
    }
)

#: Construction sites that predate the governed factory, as
#: ``module::enclosing_scope::construct``. Keyed on the enclosing scope rather
#: than a line number so ordinary edits above a site do not churn the record.
#:
#: **This record may only shrink.** An entry clears when the site moves onto the
#: factory; then delete the line. Adding an entry means adding an ungoverned
#: client, which is the thing being ratcheted down.
ALLOWED_DIRECT_CONSTRUCTION: frozenset[str] = frozenset(
    {
        "econ/analytics/budget.py::BudgetEnforcer._check_budget_status::httpx.AsyncClient",
        "econ/instrumentation/transport.py::TelemetryTransport.flush_once::httpx.Client",
        "econ/plane/connectors/registry.py::HttpJsonAdapter.send::httpx.Client",
        "governance/approvals/notifications.py::SlackNotifier.notify::httpx.AsyncClient",
        "integrations/execution/sidecar_client.py::SandboxSidecarClient.__init__::httpx.AsyncClient",
        "integrations/langgraph/_gateway_client.py::LangGraphGatewayClient.__init__::httpx.Client",
        "integrations/langgraph/_tool_decision_http.py::HttpToolDecisionClient.__init__::httpx.Client",
        "platform/artifacts/store.py::RedisArtifactStore.__init__::aioredis.from_url",
        "platform/secrets/vault.py::VaultSecretProvider._make_async_client::httpx.AsyncClient",
        "platform/secrets/vault.py::VaultSecretProvider._make_client::httpx.Client",
        "service/api/cost_api.py::register_cost_routes.get_deployment_cost::httpx.AsyncClient",
        "service/api/cost_api.py::register_cost_routes.get_tenant_cost::httpx.AsyncClient",
        "service/api/cost_api.py::register_cost_routes.set_tenant_budget::httpx.AsyncClient",
        "service/api/econ_dashboard_api.py::_dashboard_proxy::httpx.AsyncClient",
        "service/api/health.py::check_redis::aioredis.from_url",
        "service/api/health.py::check_regulus::httpx.AsyncClient",
        "service/api/regulus_proxy_api.py::_forward::httpx.AsyncClient",
        "service/bootstrap/factory.py::bootstrap_scoped_service::aioredis.from_url",
        "service/bootstrap/factory.py::bootstrap_scoped_service::httpx.AsyncClient",
        "service/langgraph_gateway/transport.py::HTTPGatewayTransport.__init__::httpx.AsyncClient",
    }
)

#: Attribute calls that construct a transport client outside the factory, keyed
#: by ``(module_alias, attribute)`` as written at the call site.
_BANNED_ATTRIBUTE_CALLS = {
    ("httpx", "AsyncClient"),
    ("httpx", "Client"),
    ("chromadb", "HttpClient"),
    ("aioredis", "from_url"),
}

#: The same constructions reached by ``from <module> import <name>``, keyed by
#: origin module and original name. Matching only the attribute form would miss
#: ``from redis.asyncio import from_url as redis_from_url`` -- which is precisely
#: how the unauthenticated readiness probe builds its unbounded Redis client, the
#: site this guard most needs to see.
_BANNED_IMPORTED_NAMES = {
    ("httpx", "AsyncClient"): "httpx.AsyncClient",
    ("httpx", "Client"): "httpx.Client",
    ("chromadb", "HttpClient"): "chromadb.HttpClient",
    ("redis.asyncio", "from_url"): "aioredis.from_url",
    ("redis.asyncio.client", "from_url"): "aioredis.from_url",
}


def _aliased_constructors(tree: ast.Module) -> dict[str, str]:
    """Map each locally-bound name back to the banned construction it performs."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            construct = _BANNED_IMPORTED_NAMES.get((node.module, alias.name))
            if construct is not None:
                aliases[alias.asname or alias.name] = construct
    return aliases


def _scope_of(tree: ast.Module) -> dict[ast.AST, str]:
    """Map each node to the dotted name of the function or class enclosing it."""
    scopes: dict[ast.AST, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}.{child.name}" if prefix else child.name
                scopes[child] = name
                walk(child, name)
            else:
                scopes[child] = prefix
                walk(child, prefix)

    walk(tree, "")
    return scopes


def _is_literal_none_timeout(call: ast.Call) -> bool:
    """Report whether *call* is ``asyncio.wait_for(..., timeout=None)`` written literally."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "wait_for"):
        return False
    return any(
        keyword.arg == "timeout"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is None
        for keyword in call.keywords
    )


def _construct_name(call: ast.Call, aliases: dict[str, str] | None = None) -> str | None:
    """Name the banned construction *call* performs, or ``None`` if it is fine."""
    if _is_literal_none_timeout(call):
        return "asyncio.wait_for(timeout=None)"
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        pair = (func.value.id, func.attr)
        if pair in _BANNED_ATTRIBUTE_CALLS:
            return f"{pair[0]}.{pair[1]}"
    if aliases and isinstance(func, ast.Name):
        return aliases.get(func.id)
    return None


def _violations_in(path: Path) -> set[str]:
    relative = path.relative_to(SOURCE).as_posix()
    if relative in GOVERNED_MODULES:
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = _scope_of(tree)
    aliases = _aliased_constructors(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        construct = _construct_name(node, aliases)
        if construct is None:
            continue
        scope = scopes.get(node, "") or "<module>"
        found.add(f"{relative}::{scope}::{construct}")
    return found


def discovered_violations() -> set[str]:
    """Every ungoverned transport construction in ``src/zeroth`` right now.

    Derived, not restated: a test that only asked "is each recorded entry still
    present" would accept any number of new ones.
    """
    found: set[str] = set()
    for path in sorted(SOURCE.rglob("*.py")):
        found |= _violations_in(path)
    return found


def test_no_ungoverned_transport_construction() -> None:
    """Exact in both directions: nothing new, and nothing stale."""
    discovered = discovered_violations()

    unrecorded = discovered - ALLOWED_DIRECT_CONSTRUCTION
    assert not unrecorded, (
        "transport clients constructed outside the governed factory: "
        f"{sorted(unrecorded)}. Construct them through the factory, which applies "
        "a mandatory non-None timeout and a shared connection lifecycle."
    )

    stale = ALLOWED_DIRECT_CONSTRUCTION - discovered
    assert not stale, (
        f"the allowlist records sites that no longer exist: {sorted(stale)}. "
        "Delete them -- a record that outlives its code stops being a ratchet."
    )


def test_the_record_can_reach_empty() -> None:
    """The allowlist must be able to be empty; that is the target state.

    A guard phrased as "the allowlist is non-empty" would refuse the one outcome
    it exists to reach.
    """
    assert isinstance(ALLOWED_DIRECT_CONSTRUCTION, frozenset)
    # Exercised over values the repository does not hold, so the rule is tested
    # rather than today's answer restated.
    for recorded, discovered, expected_ok in (
        (frozenset(), frozenset(), True),
        (frozenset({"a"}), frozenset({"a"}), True),
        (frozenset(), frozenset({"a"}), False),
        (frozenset({"a"}), frozenset(), False),
    ):
        assert ((not (discovered - recorded)) and (not (recorded - discovered))) is expected_ok


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import httpx\nc = httpx.AsyncClient()\n", {"httpx.AsyncClient"}),
        ("import httpx\nc = httpx.Client()\n", {"httpx.Client"}),
        ("c = chromadb.HttpClient(host='h')\n", {"chromadb.HttpClient"}),
        ("c = aioredis.from_url('redis://x')\n", {"aioredis.from_url"}),
        (
            "import asyncio\nasync def f(x):\n    await asyncio.wait_for(x, timeout=None)\n",
            {"asyncio.wait_for(timeout=None)"},
        ),
        ("import httpx\nc = httpx.Timeout(5)\n", set()),
        (
            "import asyncio\nasync def f(x):\n    await asyncio.wait_for(x, timeout=5)\n",
            set(),
        ),
    ],
)
def test_the_detector_sees_what_it_claims_to_see(source: str, expected: set[str]) -> None:
    """The probe, fed the constructions it exists to catch and near-misses.

    Without this the assertions above could pass because the scanner finds
    nothing at all -- which is exactly how a guard reports PASS while the
    property it names is broken.
    """
    tree = ast.parse(source)
    aliases = _aliased_constructors(tree)
    found = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and (name := _construct_name(node, aliases)) is not None
    }

    assert found == expected


def test_the_scanner_actually_reads_the_tree() -> None:
    """A scan that silently covered zero files would pass every assertion above."""
    scanned = list(SOURCE.rglob("*.py"))

    assert len(scanned) > 100, f"only {len(scanned)} source files scanned"


def test_governed_modules_exist() -> None:
    """An exemption for a path that does not exist is an exemption for nothing."""
    missing = [name for name in GOVERNED_MODULES if not (SOURCE / name).is_file()]

    assert not missing, f"exempted modules do not exist: {missing}"
