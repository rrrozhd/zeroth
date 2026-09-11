# Test suite performance audit — 2026-09-11

Removed 12 redundant tests and reduced repeated database creation, OpenAPI generation,
pytest collection, Ruff launches and audit-scanner work. Core SQLite migrations use a
session template copied into each test's private database. The affected 149
service-matrix cases went from 117.7 seconds to 8.0 seconds locally. This does not
establish the runtime of the full suite.

## What was removed

Ten pairs have identical executable bodies, arguments and decorators, with the
same relevant fixtures and bindings. Names, comments and docstrings differed.
The other two tests repeat checks already performed by broader repository scans.

| File under `tests/` | Removed test | Retained coverage |
| --- | --- | --- |
| `context_window/test_strategies.py` | `test_protocol_is_runtime_checkable` | `test_truncation_satisfies_protocol` |
| `graph/test_merge_strategy_validation.py` | `test_reducer_ref_still_checked_without_registry` | `test_custom_with_nonexistent_module_fails` uses the same validator without a registry |
| `graph/test_merge_strategy_validation.py` | `test_publish_no_parallel_config_backward_compat` | `test_publish_with_valid_graph_succeeds` publishes the same graph |
| `graph/test_mcp_tool_node.py` | `test_the_agent_is_still_a_valid_entry_step` | `test_granting_the_agent_the_floor_lets_the_import_shape_publish` |
| `http/test_client.py` | `test_no_capabilities_skips_check` | `test_get_returns_response` makes the same request without capabilities |
| `service/test_deployments_surface.py` | `test_subgraph_resolver_carries_no_canonical_deployment_import` | `runtime/test_subgraph_surface.py::test_subgraph_resolver_carries_no_deployment_service_import` runs the identical subprocess |
| `architecture/test_persistence_scope_inventory.py` | `test_public_call_inventory_clears_potential_after_bound_name_control` | `test_public_call_inventory_clears_potential_after_nonraising_if_condition` |
| `orchestrator/test_per_run_cap.py` | `test_per_run_cap_halts_run_on_next_node` | `test_per_run_cap_token_mode_halts_on_intermediate_node` constructs the same orchestrator and graph |
| `release_gates/test_gate_integrity.py` | `test_no_constant_assertion_survives_in_the_tests_tree` | `architecture/test_legacy_surface_removed.py::test_no_test_is_vacuously_true` scans the same files with the same predicate, plus other checks |
| `architecture/test_persistence_scope_inventory.py` | `test_production_audit_repository_has_only_explicit_scoped_constructors` | `test_production_audit_repository_public_calls_are_exhaustive_and_reviewed` includes the exact setup and assertion |
| `integrations/langgraph/tools/test_middleware.py` | `test_r16_a_sync_tool_never_runs_when_denied` | `test_a_denied_call_raises_the_typed_error_out_of_a_real_agent` |
| `integrations/langgraph/tools/test_middleware.py` | `test_r16_an_async_tool_never_runs_when_denied` | `test_a_denied_async_call_raises_the_typed_error_out_of_a_real_agent` |

The two duplicate repository scans alone account for 12.36 seconds in the old
timing baseline. Cheap duplicate assertions contribute little runtime; removing
them mainly reduces maintenance.

## The larger speedup

`tests/conftest.py` previously ran the complete service migration chain whenever
`async_database` was requested, including through the `sqlite_db` alias. It now
migrates one closed template per pytest process and copies that file to each
test's `tmp_path`. Database files, connections, data writes and schema changes
remain isolated. Migration-specific tests still invoke migrations directly.

A three-iteration local probe measured 0.7216 seconds per migration versus
0.000662 seconds per file copy. A separate isolation probe wrote a new table and
row to one fixture instance and verified that a second instance and the template
remained unchanged, with the migrated revision present.

The rejected alternative was sharing a writable database or widening connection
fixture scope: that would make tests depend on cleanup and execution order.

## Verification and limits

The identical command was run before and after the fixture change:

```sh
.venv/bin/python -m pytest tests/service/test_cross_tenant_leak_matrix.py \
  tests/scripts/test_openapi_generation.py -q --durations=12 \
  --junitxml=/tmp/zeroth-test-speed-after.xml
```

| Workload | Before | After |
| --- | ---: | ---: |
| 149 service-matrix cases affected by the fixture | 117.7 s | 8.0 s |
| Entire selected workload, 456 cases | 433.8 s | 180.9 s |
| Outcomes | 421 passed, 35 failed | 421 passed, 35 failed |

JUnit node IDs and outcomes were compared for exact equality. All 35 existing
failures are in the SQLAlchemy matrix and report a datetime-conversion error:
`'str' object has no attribute 'tzinfo'`. They existed before these edits and
remain unresolved. All 149 affected service-matrix cases passed in both runs.

Another 106 focused tests passed, covering retained duplicate cases, migration
behavior and the broader vacuity guard. The retained production audit-inventory
test passed separately. Ruff on changed Python files and `git diff --check` passed.

Other test processes were active on this machine, and unchanged workloads also
ran faster in the second run. Do not attribute the entire wall-time difference
to this patch or extrapolate it to the full suite. The pre-edit timing baseline
contains 12,523 records totaling 1,226.5 seconds; it is historical, not a fresh
measurement of this working tree. Only retired node IDs were pruned from
`.test_durations`; retained timings were not replaced with partial-run estimates.
This is local verification, not a completed full-suite or managed CI gate.

## Further reductions completed

- OpenAPI tests now share immutable CLI-generated reference bytes and use four
  cold processes instead of ten. Each generator still has two independent cold
  executions; the second executes `--check` against the first output. Drift is
  checked through the real CLI entry point in-process. Both former generator
  tests are one parameterized test, retaining two cases.
- The 245 SQLAlchemy matrix cases clone the schema built during collection into
  independent in-memory databases. Each new engine is disposed after its case,
  including failures. A write probe confirmed clones cannot modify the template.
- Marker integrity imports the whole test tree in one fresh interpreter, replacing
  three whole-tree and two narrower subprocess collections. It observes actual
  default selection and applies pytest's own marker selector to the same raw
  items for the other expressions. The old and new default, wheel and marked sets
  matched exactly: 14,378, 14,352 and 26 node IDs respectively. All 76 gate tests
  passed. There is no new marker-expression parser or test-exclusion policy.
- Ruff probes run the installed native binary directly. Duplicate subprocess and
  output-parsing helpers were consolidated. Positive violations, clean controls,
  exhaustive per-path enforcement and file discovery remain checked.
- The audit scanner computes local, reassigned and competing function bindings in
  one body walk instead of three. Its constructor scan also reuses one node list
  and iterates only assignment nodes when resolving aliases. Results matched the
  old helpers on every one of 8,038 source functions. All 551 scanner tests passed
  in 27.84 seconds, including the production scan and negative source fixtures.
- Both retained middleware denial tests passed with the conformance marker enabled.

Paired measurements in the same process reduce the machine-load distortion seen
in the larger before/after runs:

| Paired workload | Previous implementation | Reduced implementation |
| --- | ---: | ---: |
| Binding sets for 8,038 source functions | 6.021 s | 2.031 s |
| 40 Ruff invocations, byte-identical output | 1.144 s | 0.347 s |
| 12 schema builds/clones, isolation checked | 0.1479 s | 0.0027 s |

The first data/API run after this second pass retained the same 421 passes and
35 failures, but took 260.44 seconds while other benchmarks were running. Even
unchanged service-matrix cases slowed from 8.0 to 54.9 seconds, so that wall-time
comparison cannot isolate this patch's gain. The paired checks above establish
less work at the changed mechanisms; the full default run is recorded separately.

Retired generator timing IDs were pruned rather than assigning stale timings to
new cases. A full timing refresh needs a representative, stable environment.

Kolmogorov complexity is useful here as an intuition: keep the smallest coherent
description of independent failure modes. Identical setup and assertions can be
removed; repeated immutable setup can be shared. Merely reducing test count,
parameter count or lines does not establish that distinct faults remain covered.


## Large-data fixture reductions

The source-inventory test still checks exactly 50,000 accepted events and 50,001
rejected events against both SQLite and Postgres. The report endpoint exercises
the real full-size debugger limit. Timeline, cohorts and breakage now share a
separate two-row SQLite fixture with the loader limit set to one. They exercise
the real loader and HTTP 422 conversion; the database-dependent limit is already
covered on both engines. This removes six repeated 50,001-row loads across the
two backends. The initial expanded 32-case source-inventory run passed; the final
consolidated endpoint check passed in 2.06 seconds. Its smaller route check no
longer repeats a database-backend cross product.

The fixed-volume service evidence fixture uses scoped ORM bulk inserts instead
of constructing and tracking 20,000 ORM objects. A paired check against the old
fixture compared both inventories and every column of 10,000 execution rows and
10,000 outcome rows for exact equality. Setup took 15.985 seconds before and
2.068 seconds after. Sample sizes, statistical bounds and assertions are retained.
The file produced 32 passes and 3 skips; its separate SDK registration test could
not import the standalone SDK when this file was selected alone. That test does
not use the changed fixture and passed separately with
`PYTHONPATH=packaging/sdk/src` (1 passed in 3.32 seconds). All 33 executed
forecast-file cases therefore passed with the required SDK source available.

A managed TestClient context was evaluated for the frozen SDK workflow. Its
2-pair case passed, but the 5,000-pair case exceeded the 600-second per-test limit
while still ingesting evidence. The latest progress record showed 2,001 pairs;
the test must make 20,000 public ingestion requests before evaluation. No complete
JUnit report was produced because the thread timeout terminated the process.

That client-only optimization was reverted exactly: it did not establish a
sufficient benefit. A separate SQLite probe measured 200 ordinary commits at
0.1047 seconds versus 0.0288 seconds with savepoints. This cannot explain the
multi-minute SDK cost, so no transaction workaround was added. The SDK test's
original source, both sample sizes, frozen generator and acceptance scope remain
unchanged. Its successful mixed-outcome, 5,000-pair public workflow is not an exact
duplicate of the all-correct service fixture; deleting it would lose coverage.

## Full-suite measurement remains incomplete

The default run was intentionally interrupted after 946.21 seconds (15m46s), with
5,320 passed, 22 failed, 7 skipped and 463 deselected. It had completed roughly
37% of the selected cases. This is an incomplete run, not a passing full-suite
gate or a measured final runtime. It preceded the large-data fixture changes
above. JUnit and timings are in `/tmp/zeroth-minimize-full.xml` and
`/tmp/zeroth-minimize-full-durations.json`.

Observed failures include absent Anthropic/LangChain dependencies, contracts that
still pin the prior migration revision, schema snapshots that differ from the
current working tree, and packaging checks rejecting untracked forecast files.
These are separate from the earlier 35 SQLAlchemy matrix failures. The interrupted
full run does not establish that every failure predates this patch.

Current expensive work includes 50,000-event database boundaries, full public SDK
ingestion, real clean-install/acceptance checks and production-tree audits. The
historical timing file omits many current cases. Repeated local runs also show
substantial host contention; unchanged qualification work ranged from about 49
to 198 seconds. No full-suite speedup percentage is established.


## Delivered scope

No production code, dependencies, CI exclusions or statistical sample sizes were
changed by this performance work. The pre-existing forecast work remains in the
working tree; only its test evidence fixture was optimized, with exact row
comparison. SDK acceptance experiments were reverted. Ruff on the changed Python
files and `git diff --check` pass. The full-suite runtime and full-suite green
status remain unresolved. The report distinguishes retained improvements from
unsuccessful experiments rather than claiming the suite is globally minimized.


## Commit boundary

The commit contains the independent tracked-test changes and the shared SQLite
fixture documentation. The bulk optimization in
`tests/econ_plane/test_fixed_volume_forecasts.py` remains local with the new,
uncommitted forecast feature. Committing that entire test file alone would add
tests for production modules absent from the committed tree. Its measurements
above describe local work, not code included in this commit.


## Commit verification

An isolated checkout of the staged code, excluding the unrelated forecast work,
passed 971 tests in 48.94 seconds. The selection covered the full audit-inventory
file, the full cross-tenant matrix and the consolidated debugger endpoint check.
Ruff passed across `src` and `tests`. This establishes the selected commit checks;
it does not replace the incomplete full-suite measurement described above.
