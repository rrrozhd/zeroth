# Migration from the monolith layout

If you have a codebase that imports from the pre-split monolithic `zeroth.*` namespace, this guide walks you through the one-time upgrade to the published `zeroth-core` package under `zeroth.core.*`. The change is a pure rename — no runtime semantics change, no API breaks.

The rename was introduced in the v3.0 milestone.

## TL;DR

1. `pip install "zeroth-core>=0.1.1"` (drop any local/path dependency on `zeroth`)
2. Rewrite imports: `from zeroth.X` → `from zeroth.core.X`
3. Drop any local path dependency on `econ-instrumentation-sdk` — it's now a transitive PyPI dep of `zeroth-core`
4. Keep all `ZEROTH_*` env vars as-is — no renames
5. Rebuild your Docker image against the new package name

Most small projects complete this migration in under 10 minutes.

## 1. Install the published package

**Before** (monolith, path-installed):

```bash
pip install -e /path/to/zeroth-monolith
```

**After** (PyPI):

```bash
pip install "zeroth-core>=0.1.1"
# Or with extras matching your backend:
pip install "zeroth-core[memory-pg,dispatch]>=0.1.1"
```

If your project pins zeroth in `pyproject.toml`, change:

```toml
# Before
dependencies = [
  "zeroth @ file:///path/to/zeroth-monolith",
]

# After
dependencies = [
  "zeroth-core>=0.1.1",
]
```

Note the distribution name is `zeroth-core` but you import it as `zeroth.core.*` (PEP 420 namespace package). Available extras match the monolith: `memory-pg`, `memory-chroma`, `memory-es`, `dispatch`, `sandbox`, and `all`.

## 2. Rewrite imports

Every import from `zeroth.<subsystem>` becomes `zeroth.core.<subsystem>`.

**Before:**

```python
from zeroth.orchestrator import Orchestrator
from zeroth.graph import Graph, Node
from zeroth.memory import EphemeralMemory
import zeroth.policy as policy
```

**After:**

```python
from zeroth.core.orchestrator import Orchestrator
from zeroth.core.graph import Graph, Node
from zeroth.core.memory import EphemeralMemory
import zeroth.core.policy as policy
```

### Grep + sed recipe

For a quick in-place rename across a project, the following works on macOS (BSD sed) and Linux (GNU sed — remove the empty `''` after `-i`):

```bash
# macOS (BSD sed)
grep -rl "from zeroth\." src/ tests/ | xargs sed -i '' 's/from zeroth\./from zeroth.core./g'
grep -rl "import zeroth\." src/ tests/ | xargs sed -i '' 's/import zeroth\./import zeroth.core./g'

# Linux (GNU sed)
grep -rl "from zeroth\." src/ tests/ | xargs sed -i 's/from zeroth\./from zeroth.core./g'
grep -rl "import zeroth\." src/ tests/ | xargs sed -i 's/import zeroth\./import zeroth.core./g'
```

**Caveat:** this is a naive substitution. It will correctly rewrite `from zeroth.graph` and `import zeroth.runtime`, but will also match false positives like string literals (`"zeroth.something"`) and dotted paths inside YAML/TOML configs or docstrings. Always diff before committing:

```bash
git diff --stat
git diff src/ tests/ | less
```

A future release will ship a LibCST-based codemod (`python -m zeroth.core.codemods.rename_from_monolith`) that handles edge cases correctly — tracked internally as `FUTURE-01`.

### Verify the rewrite

```bash
# Should return zero matches after the rewrite.
grep -rn "^from zeroth\." src/ tests/
grep -rn "^import zeroth\." src/ tests/

# Run your test suite.
uv run pytest
```

If either `grep` prints anything, the offending lines still reference the monolith namespace — fix them by hand or rerun the sed recipe against the remaining paths.

## 3. econ SDK path swap

The monolith depended on `econ-instrumentation-sdk` via a local file path. Zeroth-core pins it as a published PyPI dependency:

```toml
# In zeroth-core's pyproject.toml (transitive — you don't need to declare it)
"econ-instrumentation-sdk>=0.1.1",
```

**Action:** remove any local path dep from your own `pyproject.toml`:

```toml
# Before
dependencies = [
  "econ-instrumentation-sdk @ file:///path/to/regulus/sdk",
  "zeroth @ file:///path/to/zeroth-monolith",
]

# After
dependencies = [
  "zeroth-core>=0.1.1",
]
```

`zeroth-core` will install `econ-instrumentation-sdk` automatically. If you need to override the version for testing, pin it explicitly alongside zeroth-core:

```toml
dependencies = [
  "zeroth-core>=0.1.1",
  "econ-instrumentation-sdk==0.1.1",
]
```

After the swap, run `uv sync` (or `pip install -e .`) and confirm only one `econ-instrumentation-sdk` shows up:

```bash
uv pip list | grep econ-instrumentation-sdk
```

## 4. Environment variables

**No env vars were renamed during the rename phase.** All `ZEROTH_*` settings keep their names, their nesting conventions (`ZEROTH_<SECTION>__<FIELD>`), and their defaults. This includes:

- `ZEROTH_DATABASE__POSTGRES_DSN`
- `ZEROTH_REDIS__*`
- `ZEROTH_REGULUS__*`
- `ZEROTH_AUTH__API_KEYS_JSON`
- …and every other nested `ZEROTH_*` key your monolith deployment already used.

See the full [Configuration Reference](../reference/configuration.md) for every supported variable.

If your deployment sets `ZEROTH_*` vars via `.env`, docker-compose, Kubernetes secrets, or a systemd unit file, **no changes are required**. The monolith and `zeroth.core` layouts share the exact same pydantic-settings schema.

## 5. Docker image retag

Zeroth-core does not currently publish an official Docker image — you build your own from the package, or use the `docker-compose.yml` in the `zeroth-core` repo as a reference.

**If you had a Dockerfile for the monolith** that installed it in editable mode, replace the install step:

```dockerfile
# Before
COPY zeroth-monolith /src/zeroth-monolith
RUN pip install -e /src/zeroth-monolith

# After
RUN pip install "zeroth-core[memory-pg,dispatch]>=0.1.1"
```

**Retag** your image (the tag is arbitrary — pick one that matches your registry layout):

```bash
docker build -t registry.example.com/myorg/myapp:zeroth-core-0.1.1 .
docker push registry.example.com/myorg/myapp:zeroth-core-0.1.1
```

Update your Kubernetes manifests, Helm values, or docker-compose files to point at the new tag, and roll the deployment. Because env vars are unchanged (see Section 4), no ConfigMap/Secret edits are needed.

For a ready-made compose file, see the [docker-compose deployment guide](deployment/docker-compose.md).

## 6. Verify the migration

Run your existing test suite. The rename is purely structural with zero functional changes, so all passing tests on the monolith should still pass on `zeroth-core` without edits:

```bash
uv run pytest
```

Then smoke-test the service layer against your own graphs:

```bash
uv run zeroth-core serve  # or however you launch your app
curl http://localhost:8000/healthz
```

If a test fails with an `ImportError` mentioning `zeroth.<something>` (without `.core`), the rename missed that file — rerun the sed recipe or fix manually.

## Troubleshooting

- **`ModuleNotFoundError: No module named 'zeroth'`** after install — PEP 420 namespace package: make sure nothing in your project creates a `zeroth/__init__.py` that would shadow the namespace. The `zeroth-core` wheel intentionally ships no top-level `__init__.py`.
- **`ModuleNotFoundError: No module named 'zeroth.orchestrator'`** — rename missed; check for imports in `.pyi` stub files, `conftest.py`, plugin entry points in `pyproject.toml`, and any YAML/TOML config referencing dotted module paths.
- **Duplicate `econ-instrumentation-sdk` install** — remove the local path dep from your `pyproject.toml`; the PyPI version pulled in by `zeroth-core` is canonical.
- **My CI is still using the monolith wheel** — clear your CI's pip cache and pin `zeroth-core>=0.1.1` explicitly. If you use `uv`, delete `uv.lock` and re-run `uv lock`.
- **Docstring or comment still says `zeroth.foo`** — the sed recipe only targets `from`/`import` lines; fix prose references by hand as you find them.

## What's not covered

This guide only covers the monolith → `zeroth.core.*` rename. Future migrations (e.g., between `zeroth-core` minor versions) will get their own per-release guides as they ship. The CHANGELOG on the `zeroth-core` repo is the canonical source for version-to-version upgrade notes.
