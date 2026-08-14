"""The orchestration interrupt surface is dormant, and stays dormant on purpose.

``RedisInterruptStore.sweep_expired`` reaps expired interrupt payloads and the
index entries naming them. ZER-49 repaired it: it now maintains the index it
invalidates, so ``list_requests`` no longer heals the index from a read path and
can no longer erase a concurrent ``save_request``. That repair is correct and
stands on its own.

**It is deliberately not scheduled.** The reason is upstream of scheduling: no
``src`` path writes the keyspace it would sweep.

* ``RedisInterruptStore`` is constructed in exactly one place,
  ``build_governai_redis_runtime`` -- and that factory has no ``src`` callers.
  It is not re-exported from ``zeroth/__init__.py`` or named in any how-to, so
  it is not an embedder entry point either; its ``__all__`` entry and surface
  test pin it as "do not move", which protects vestigial symbols as readily as
  live ones.
* ``InterruptManager.create``, the only writer of an ``InterruptRequest``, has
  no ``src`` callers.
* The one ``InterruptManager`` built in ``src`` (``admin_api``) is per-request
  with the default in-memory store, and is used only for pause/resume/cancel --
  all three delegate to the token lifecycle adapter and never touch
  ``self.store``. That store is empty for its whole lifetime, and the third pin
  below keeps it that way: ``admin_api`` is an allowed importer, so a writer
  appearing *inside* it is the one path the other two pins cannot see.
* The live human-in-the-loop path is a different subsystem entirely:
  ``zeroth.governance.approvals`` plus the LangGraph ``_approval_lifecycle``.
  Neither imports this module.

So a scheduled sweep would SCAN the entire keyspace, twice per tick, to find
nothing -- and giving it something to find would mean constructing a Redis
interrupt store in bootstrap purely as a target, fabricating a production
persistence path the product does not have.

**The wiring, for whoever trips these pins.** Follow ``RetentionPurgeWorker``
(``zeroth/governance/retention/worker.py``) exactly; do not invent a mechanism.
A ``sweep_once()``/``poll_loop()`` component started from the service lifespan
next to the retention worker, gated on the store via the established
``getattr(bootstrap, ..., None)`` idiom, as a named task cancelled in teardown.
Not arq: that surface is a run-dispatch wakeup consumer, not a periodic
scheduler. Hourly, mirroring ``RetentionSettings`` (``enabled`` plus a ``gt=0``
interval); interrupt TTL defaults to 1800s, and expiry is already enforced on
the read path by ``resolve`` raising ``InterruptExpiredError`` -- the sweep
reclaims storage, it is not what makes expiry correct. Overrun is a non-issue:
``poll_loop`` sleeps *after* awaiting the sweep, so a slow sweep delays the next
one instead of overlapping it; never switch to a fixed-rate timer. Supervision
must re-raise ``asyncio.CancelledError`` before swallowing ``Exception`` with
``logger.exception``, or shutdown cancellation is caught by the bare handler and
the loop spins on. Concurrent replicas are benign and want no lease: ``GET`` +
``DELETE`` of an expired payload and ``LREM`` of its id are idempotent, two
replicas racing produce one delete and one miss, and the returned count is a
metric rather than a ledger -- a lease would trade that harmless race for a
crashed holder blocking every sweep. Do not schedule
``InterruptManager.clear_expired`` instead: it is per-run, costs k+1 sequential
round-trips for k expiries, and also deletes *resolved* requests, which is a
different policy than expiry.

These are invariant pins, not negative controls -- they pass before and after
the decision they record, because the decision was to add no behavior. Their job
is to fail the day someone wires a writer, so the scheduling question gets
answered then, against a real arrangement, rather than now against a
hypothetical one.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "zeroth"

INTERRUPTS_MODULE = "zeroth.runtime.orchestration.interrupts"

# The two modules that may touch the interrupt surface today.
#
# ``governed_redis`` builds a store nothing asks it for; ``admin_api`` builds an
# ``InterruptManager`` per request for pause/resume/cancel only -- those three
# delegate to the token lifecycle adapter and never touch ``self.store``, so its
# default in-memory store is empty for its whole lifetime.
ALLOWED_IMPORTERS = {
    ROOT / "integrations" / "persistence" / "governed_redis.py",
    ROOT / "service" / "api" / "admin_api.py",
}

STORE_FACTORY = "build_governai_redis_runtime"

MANAGER = "InterruptManager"


def _module_paths() -> list[Path]:
    return sorted(ROOT.rglob("*.py"))


def _called_name(node: ast.Call) -> str | None:
    """The bare callable name behind a call, ignoring how it was qualified."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _imports_interrupts(tree: ast.AST) -> bool:
    """Whether this module imports the orchestration interrupts module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == INTERRUPTS_MODULE:
            return True
        if isinstance(node, ast.Import) and any(
            alias.name == INTERRUPTS_MODULE for alias in node.names
        ):
            return True
    return False


def test_only_two_modules_touch_the_interrupt_surface() -> None:
    """No third module imports the interrupt surface without re-deciding the sweep.

    A new importer is the signal that someone is building on this surface. That
    is the moment to decide whether the sweeper needs scheduling -- not before,
    when there is nothing to sweep.
    """
    importers = {
        path
        for path in _module_paths()
        if _imports_interrupts(ast.parse(path.read_text(), filename=str(path)))
    }

    unexpected = sorted(str(path.relative_to(ROOT)) for path in importers - ALLOWED_IMPORTERS)
    assert not unexpected, (
        "new src importers of the dormant interrupt surface: "
        + ", ".join(unexpected)
        + " -- if this one writes interrupts, the expiry sweep now has a keyspace and "
        "needs scheduling; this module's docstring carries the wiring and the reasoning"
    )

    departed = sorted(str(path.relative_to(ROOT)) for path in ALLOWED_IMPORTERS - importers)
    assert not departed, (
        "expected importers are gone: "
        + ", ".join(departed)
        + " -- if the interrupt surface is being retired, retire this pin with it"
    )


def test_the_redis_interrupt_store_is_never_constructed() -> None:
    """The only ``RedisInterruptStore`` constructor has no ``src`` callers.

    ``build_governai_redis_runtime`` instantiates one, so the store type alone is
    not evidence of a live keyspace; an actual call to the factory is. Nothing
    calls it, which is why there is nothing to sweep.
    """
    callers: list[str] = []
    for path in _module_paths():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) == STORE_FACTORY:
                callers.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not callers, (
        f"{STORE_FACTORY} now has src callers: "
        + ", ".join(callers)
        + " -- a Redis interrupt keyspace now exists, so its expiry sweep needs a "
        "schedule; this module's docstring carries the wiring and the reasoning"
    )


def test_the_interrupt_manager_is_never_given_a_store() -> None:
    """Every ``src`` ``InterruptManager`` keeps its default in-memory store.

    The other two pins watch for a *new* module reaching the surface. This one
    watches the module already holding an ``InterruptManager`` -- ``admin_api``,
    an allowed importer, and so the likeliest place a writer appears without
    tripping anything else. Passing ``store=`` is what would bind one to durable
    storage and give the expiry sweep a real keyspace.

    Persisting through the default in-memory store would still not survive the
    request that created it, so this pin plus the importer pin together cover
    the ways the surface can start writing something a sweep would need to reap.
    """
    bound: list[str] = []
    for path in _module_paths():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _called_name(node) != MANAGER:
                continue
            if any(keyword.arg == "store" for keyword in node.keywords):
                bound.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not bound, (
        f"{MANAGER} is now constructed with an explicit store: "
        + ", ".join(bound)
        + " -- interrupts may now be persisted, so their expiry needs a schedule; "
        "this module's docstring carries the wiring and the reasoning"
    )
