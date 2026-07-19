# Blocker: eager `zeroth.core` imports prevent extracting persistence

**Status:** RESOLVED 2026-07-18 by option 1 (make the platform layer
import-clean). Discovered the same day while starting Task 6 (split concrete
run persistence). Kept as the record of why the lazy package `__init__` files
exist — reverting them re-blocks Task 6.

Outcome: importing `zeroth.core.storage` went from **130 modules to 18**, and
`zeroth.core.runs`, `agent_runtime`, `audit`, and `memory` are no longer
reached at all. An extracted persistence module that imports the run models is
now importable from both the integrations side and the core side.

Enforced by `tests/architecture/test_import_layering.py`. See
"Resolution" at the end of this document.

## Symptom

Extracting any part of `zeroth/core/runs/repository.py` into
`zeroth.integrations.persistence.runs` produces a circular import as soon as
the legacy repository delegates to the extracted module:

```
ImportError: cannot import name 'dump_list' from partially initialized module
'zeroth.integrations.persistence.runs.serialization'
(most likely due to a circular import)
```

The extracted module needs the run models. Reaching them — by *any* path,
`zeroth.runtime.runs` or `zeroth.core.runs.models` — executes `zeroth.core`'s
import graph, which reaches `zeroth.core.runs.repository`, which imports the
extracted module while it is still initializing.

## Root cause: the lowest layer transitively imports the highest

Importing `zeroth.core.storage` — nominally platform, the bottom of the
dependency matrix — pulls in **135 `zeroth` modules**, including:

| Module | Domain | Should storage depend on it? |
| --- | --- | --- |
| `zeroth.core.runs`, `.runs.models`, `.runs.repository` | runtime / integrations | No |
| `zeroth.core.agent_runtime` | runtime | No |
| `zeroth.core.econ` | econ | No |
| `zeroth.core.audit` | governance | No |
| `zeroth.core.memory` | integrations | No |

The entry chain:

```
zeroth/core/__init__.py:13   -> zeroth.core.storage
storage/__init__.py:17       -> storage.factory
storage/factory.py:9         -> zeroth.core.config.settings
config/settings.py:22        -> zeroth.core.econ.models      (for RegulusSettings)
                                 ...which executes econ/__init__.py, eagerly importing
econ/__init__.py:20          -> econ.rightsizing_experiment
rightsizing_experiment.py:36 -> zeroth.core.agent_runtime.provider, audit.models
                                 ...and econ.quality -> zeroth.core.runs.models
```

`zeroth/core/__init__.py` itself is innocent — 52 lines, only storage imports,
already lazy for the optional Postgres backend. The inversion lives below it.

Two of these edges are already recorded as architectural debt in
`src/zeroth/_architecture.py`:

```python
("zeroth.core.config.settings", "zeroth.core.econ.models"),
("zeroth.core.config.settings", "zeroth.core.http.models"),
# removal_task="Task 11: move platform packages and relocate domain-aware wiring."
```

So the plan already knows about this coupling. What was not anticipated is that
**Task 6 needs it resolved first**, because Task 6 is the first task to put a
non-`zeroth.core` module on `zeroth.core`'s own import path.

## What was ruled out

Making `zeroth/core/econ/__init__.py` lazy (dropping the eager
`rightsizing_experiment` import) was tested directly and **does not fix it** —
the cycle reproduces unchanged. There is more than one route from the eager
chain into `zeroth.core.runs`, so a single-import fix is insufficient. Any
proposal here needs the full route set enumerated first, not a spot fix.

## Why the test suite cannot see this

`tests/conftest.py` imports `zeroth.core.service.bootstrap` at collection time,
so `zeroth.core` is always warm before any test module loads. Every cycle of
this class passes the full suite. The guards that catch it are the subprocess
cold-import tests — currently
`tests/runtime/test_run_contracts.py::test_canonical_package_imports_in_a_cold_interpreter`.
**Every canonical package needs one as it is published.**

## Options

1. **Insert a prerequisite task: make the platform layer import-clean.**
   Enumerate every route from `zeroth.core.storage`/`config` into higher
   domains and break them (lazy `__getattr__` on the offending package inits,
   or moving the settings models that `config.settings` needs). This is
   Task 11 work pulled forward, and it unblocks Tasks 6–16 rather than just
   Task 6. Largest scope, highest leverage.

2. **Extract with deferred imports.** Have the new persistence modules import
   models inside function bodies rather than at module scope, so they import
   nothing from `zeroth.core` at load time. Contained and unblocks Task 6
   immediately, but pushes an unusual import style into new code that has to be
   undone once option 1 lands.

3. **Reorder Task 6 to delete rather than delegate.** Skip the intermediate
   state where `core/runs/repository.py` delegates to the new modules: move the
   implementation and rewire every consumer to the new location in one step, so
   the legacy module never imports the new package. Avoids the cycle but gives
   up the plan's incremental red-green slices and produces one very large
   commit.

Recommendation: option 1. The inverted dependency is real debt that every
remaining move task will hit, the plan already schedules its removal, and
options 2 and 3 both spend effort that option 1 makes unnecessary.

## Resolution

Option 1, in two commits. The full crossing-edge set turned out to be small:
seven edges from two platform modules, which is why the earlier single-import
probe failed — `econ/__init__.py` eagerly imported `quality`, `opportunities`,
*and* `rightsizing_experiment`, and only one was cut.

**`refactor: keep the platform layer out of higher domains`**

- `zeroth.core.econ` and `zeroth.core.http` resolve their exports lazily.
  `config.settings` needs one settings class from each, and importing a
  submodule executes its package `__init__`. Both packages already used a
  `__getattr__` for the single symbol that previously cycled; that pattern now
  covers the whole public API.
- `zeroth.core.storage.redis` moves its governed-store imports into the factory
  that builds the stores, so the storage layer no longer imports governance and
  runtime code at module scope.
- `tests/architecture/test_import_layering.py` asserts the closure from
  subprocesses, since the in-process suite always has `zeroth.core` warm.

**`refactor: resolve run repositories lazily`**

- `zeroth.core.runs` resolves `RunRepository`/`ThreadRepository` on first
  access, so reading a run model no longer loads the SQL adapter.

### What stayed, and why

Four leaf modules — `zeroth.core.econ`, `.econ.models`, `zeroth.core.http`,
`.http.models` — were still loaded by the platform layer at the time of this
analysis, listed explicitly in `PERMITTED_NON_PLATFORM` with a test that fails
if one stops being needed. `ZerothSettings` composes `RegulusSettings` and
`HttpClientSettings` as fields, and its signature string — which embedded their
module paths — is pinned in the immutable legacy surface, which at the time
forbade relocating them.

**Resolved by Task 11.** Signature comparison became location-independent in
54378e7 (Task 10), so the two settings sections moved into
`zeroth.platform.config.models` with the config package; the legacy
`zeroth.core.econ.models` and `zeroth.core.http.models` paths republish the
platform-owned classes. `PERMITTED_NON_PLATFORM` is empty again.

### One Python subtlety worth knowing before touching these files

`zeroth.core.econ` exports a function named `unit_economics` from a submodule of
the same name. Importing a submodule binds it as an attribute of its package,
and CPython does that **after** the submodule finishes executing — so neither a
lazy loader's cache nor a module-level `__getattr__` (PEP 562) can win the race,
and asserting the binding from inside the submodule does not work either. The
eager imports used to resolve this as a side effect of import order.

A `ModuleType` subclass assigned to the package's `__class__` now keeps the
function bound. Every import form was compared against the pre-change behavior
and matches, including `import zeroth.core.econ.unit_economics as m`, which
resolved to the *function* before this change too.

Renaming the colliding submodule was considered and rejected: models defined in
it are referenced in signatures pinned by the immutable legacy fixture.
