# Blocker: eager `zeroth.core` imports prevent extracting persistence

**Status:** open, blocks Task 6 onward. Discovered 2026-07-18 while starting
Task 6 (split concrete run persistence). Task 5 is complete and unaffected.

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
