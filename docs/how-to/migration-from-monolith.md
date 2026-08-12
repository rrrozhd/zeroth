# Migration from the monolith layout

If you have a codebase that imports from the pre-split monolithic `zeroth.*`
namespace, this guide walks you through the one-time upgrade to the published
`zeroth-core` package. The current package is split across `zeroth.runtime`,
`zeroth.contracts`, `zeroth.integrations`, `zeroth.governance`, and other
subsystems, so migrate each import by responsibility rather than applying a
global prefix rename.

The legacy import surface was removed in release 0.17.

## TL;DR

1. `pip install zeroth-core` (drop any local/path dependency on `zeroth`)
2. Rewrite imports using the mappings below
3. Drop any local path dependency on `econ-instrumentation-sdk`; its code is bundled in `zeroth-core`
4. Check renamed environment variables against the generated configuration reference
5. Rebuild your Docker image against the new package name

Most small projects complete this migration in under 10 minutes.

## 1. Install the published package

**Before** (monolith, path-installed):

```bash
pip install -e /path/to/zeroth-monolith
```

**After** (PyPI):

```bash
pip install zeroth-core
# Or with extras matching your backend:
pip install "zeroth-core[memory-pg,dispatch]"
```

If your project pins zeroth in `pyproject.toml`, change:

```toml
# Before
dependencies = [
  "zeroth @ file:///path/to/zeroth-monolith",
]

# After
dependencies = [
  "zeroth-core",
]
```

The distribution name is `zeroth-core`; its modules share the PEP 420 `zeroth`
namespace. See the root `pyproject.toml` for the current optional extras.

## 2. Rewrite imports

Map each old subsystem to its current owner. The examples below cover the
common orchestration, graph, memory, and policy imports.

**Before:**

```python
from zeroth.orchestrator import Orchestrator
from zeroth.graph import Graph, Node
from zeroth.memory import EphemeralMemory
import zeroth.policy as policy
```

**After:**

```python
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.contracts.graph import Graph, Node
from zeroth.integrations.memory import RunEphemeralMemoryConnector
import zeroth.governance.policy as policy
```

### Verify the rewrite

```bash
# Review every remaining Zeroth import against the current package tree.
rg '^(from|import) zeroth\.' src tests

# Run your test suite.
uv run pytest
```

Review each match by hand; current imports still begin with `zeroth.`, so a raw
substring search cannot distinguish a migrated import from a legacy one.

## 3. Econ instrumentation path swap

The instrumentation client now ships inside `zeroth-core`. Remove any local
`econ-instrumentation-sdk` dependency from your own `pyproject.toml`:

```toml
# Before
dependencies = [
  "econ-instrumentation-sdk @ file:///path/to/regulus/sdk",
  "zeroth @ file:///path/to/zeroth-monolith",
]

# After
dependencies = [
  "zeroth-core",
]
```

Import the bundled client from `zeroth.econ.instrumentation`; no separate
distribution is installed or versioned.

## 4. Environment variables

Most settings use the nested `ZEROTH_<SECTION>__<FIELD>` convention. Service
authentication is a maintained flat override, so check old deployments in
particular for:

- `ZEROTH_DATABASE__POSTGRES_DSN`
- `ZEROTH_SERVICE_API_KEYS_JSON`

See the full [Configuration Reference](../reference/configuration.md) for every supported variable.

Compare every `.env`, Compose, Kubernetes, or systemd key with that generated
reference before rollout; it reflects the settings schema shipped by the
current checkout.

## 5. Docker image retag

Zeroth-core does not publish an official Docker image — you build your own from the package. See the [sandbox container guide](deployment/sandbox-container.md) for the isolation-focused recipe.

**If you had a Dockerfile for the monolith** that installed it in editable mode, replace the install step:

```dockerfile
# Before
COPY zeroth-monolith /src/zeroth-monolith
RUN pip install -e /src/zeroth-monolith

# After
RUN pip install "zeroth-core[memory-pg,dispatch]"
```

**Retag** your image (the tag is arbitrary — pick one that matches your registry layout):

```bash
docker build -t registry.example.com/myorg/myapp:zeroth-core .
docker push registry.example.com/myorg/myapp:zeroth-core
```

Update your Kubernetes manifests, Helm values, or Docker Compose files to point
at the new tag, and apply any environment-variable changes found in Section 4.

## 6. Verify the migration

Run your existing test suite. The rename is purely structural with zero functional changes, so all passing tests on the monolith should still pass on `zeroth-core` without edits:

```bash
uv run pytest
```

Then smoke-test the service layer against your own graphs:

```bash
uv run zeroth-core serve  # or however you launch your app
curl http://localhost:8000/health/ready
```

If a test fails with an `ImportError` naming a retired top-level module such as
`zeroth.orchestrator`, `zeroth.graph`, `zeroth.memory`, or `zeroth.policy`, that
import still needs the responsibility-based rewrite from Section 2.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'zeroth'`** after install — PEP 420 namespace package: make sure nothing in your project creates a `zeroth/__init__.py` that would shadow the namespace. The `zeroth-core` wheel intentionally ships no top-level `__init__.py`.
- **`ModuleNotFoundError: No module named 'zeroth.orchestrator'`** — rename missed; check for imports in `.pyi` stub files, `conftest.py`, plugin entry points in `pyproject.toml`, and any YAML/TOML config referencing dotted module paths.
- **Duplicate `econ-instrumentation-sdk` install** — remove the old local or
  published dependency; the implementation is part of `zeroth-core`.
- **My CI is still using the monolith wheel** — clear its pip cache, require
  `zeroth-core`, and regenerate the lock file with your package manager.
- **Docstring or comment still names an old module** — update prose references
  by hand after the import migration.

## What's not covered

This guide covers the removed monolith import surface. The CHANGELOG is the
canonical source for later version-to-version upgrade notes.
