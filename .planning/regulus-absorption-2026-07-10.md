# Regulus absorption — drop the vendored/re-sync model, single owner

**Decision (user, 2026-07-10):** Regulus is being absorbed into Zeroth. It is no
longer a separately-maintained external product that we vendor and re-sync — Zeroth
owns the economic control plane outright. Fold the two `src/econ_*` trees into the
`zeroth` namespace as internal subpackages and drop the re-sync fiction.

## Current state (verified)

Three econ trees, one process (monolith; Regulus mounts in-process at `/regulus` or
is absent):

| Tree | LOC | Files | Internal import lines | External consumers |
|---|---|---|---|---|
| `src/zeroth/core/econ/` (native, Zeroth-owned) | 2541 | — | — | — |
| `src/econ_instrumentation/` (Regulus SDK) | 1905 | 16 | 39 | `econ/client.py`, `econ/adapter.py`, tests |
| `src/econ_plane/` (Regulus backend) | 4629 | 71 | 138 | `service/app.py`, `econ/service_auth.py`, test |

- The two vendored trees are **independent** — no cross-imports — so they move separately.
- `econ_instrumentation` runtime deps: pydantic + httpx (already core). `econ_plane`
  boot deps live in the `regulus` optional-extra (unchanged).
- `econ_plane` has its own `ECP_`-prefixed config, its own SQLite DB
  (`econ_plane.db`), its own JWT auth, and Alembic migrations under `_migrations/`.

## Target layout

- `src/econ_instrumentation/` → `src/zeroth/core/econ/instrumentation/`
  (import path `zeroth.core.econ.instrumentation`). It is the client-side cost SDK,
  used by the Zeroth cost path regardless of whether the backend is installed —
  belongs next to core econ.
- `src/econ_plane/` → `src/zeroth/econ_plane/`
  (import path `zeroth.econ_plane`). A full subsystem (DB/auth/migrations/FastAPI
  app), a sibling of `zeroth.core`, not econ-lens code — so it sits at `zeroth.*`,
  not nested under `core.econ`.

Result: everything ships under `src/zeroth`; the wheel `packages` list collapses to
`["src/zeroth"]` (PEP 420 namespace — re-verify the build).

## Deliberate NON-changes (out of scope for the move)

- **Keep `ECP_` env prefix** — renaming is a config-breaking change to every
  deployment; absorption is about code ownership, not the config surface. Revisit
  separately if desired.
- **Keep the `/regulus` mount path and the `regulus` optional-extra name** — public
  surface, behavior unchanged.
- **Keep a lint / interrogate exclude on the absorbed trees**, but reword the
  rationale from "vendored, keep upstream style for clean re-sync" to "large absorbed
  subsystem; Zeroth-owned; full lint-conformance deferred." Bringing 6500 LOC to
  Zeroth's `D`/ruff standard is a separate effort.
- **Keep the SQLite filename `econ_plane.db`** and any prose/log strings — the
  import rewrite touches Python module references only, never the db filename.

## Steps

1. `git mv src/econ_instrumentation src/zeroth/core/econ/instrumentation`; rewrite the
   39 internal `econ_instrumentation.*` module refs → `zeroth.core.econ.instrumentation.*`;
   rewrite the 3 consumers. Smoke: `import zeroth.core.econ.instrumentation`.
2. `git mv src/econ_plane src/zeroth/econ_plane`; rewrite the 138 internal
   `econ_plane.*` module refs → `zeroth.econ_plane.*` (+ the 1 alembic ref); rewrite
   `app.py` + `service_auth.py`. Guard against touching `econ_plane.db` / prose.
   Smoke: `import zeroth.econ_plane.main`.
3. pyproject: wheel `packages` → `["src/zeroth"]`; repoint ruff `extend-exclude` and
   interrogate `exclude` to the new paths; update the `regulus` extra comment. Rewrite
   both `VENDOR.md` files into short provenance/absorbed notes (drop the re-sync
   procedure).
4. Verify: import smoke (both trees), full `uv run pytest`, `uv build` wheel sanity
   (namespace packing intact), `ruff check src/zeroth` (excluding absorbed trees),
   and a Regulus-enabled boot smoke if feasible.
5. Version bump `0.9` → `0.10` (High — architectural namespace restructure of a
   subsystem). No commit without explicit authorization.

## Risks

- Import-rewrite over-reach (the `econ_plane.db` filename, log/prose strings) → use
  precise patterns anchored to `from `/`import `/dotted-module refs; review `git diff`.
- Wheel packaging regression (namespace layout was verified once against the 0.1.0
  wheel) → re-verify `uv build`.
- Alembic `env.py` package reference under `_migrations/`.
