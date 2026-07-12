# Runtime Dispatch Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent graph runs from mutating or observing another run's agent-runner state.

**Architecture:** Registered `AgentRunner` objects become immutable prototypes. `fork_for_dispatch()` creates a shallow dispatch-local runner while rebuilding every mutable bridge/session holder; the orchestrator mutates only the fork.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, pytest-asyncio.

---

### Task 1: Specify runner fork ownership

**Files:**
- Modify: `src/zeroth/core/agent_runtime/runner.py`
- Modify: `src/zeroth/core/context_window/tracker.py`
- Test: `tests/agent_runtime/test_runner_dispatch_isolation.py`

- [ ] **Step 1: Write the failing ownership test**

```python
def test_fork_for_dispatch_rebuilds_mutable_state(base_runner):
    fork = base_runner.fork_for_dispatch()
    assert fork is not base_runner
    assert fork.config == base_runner.config
    assert fork.tool_bridge is not base_runner.tool_bridge
    assert fork._mcp_manager is None
    assert fork.context_tracker is not base_runner.context_tracker
    assert fork.context_tracker.settings == base_runner.context_tracker.settings
    assert fork.context_tracker.state.compaction_count == 0
    fork.config = fork.config.model_copy(update={"instruction": "local"})
    assert base_runner.config.instruction != "local"
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/agent_runtime/test_runner_dispatch_isolation.py::test_fork_for_dispatch_rebuilds_mutable_state`
Expected: FAIL because `AgentRunner.fork_for_dispatch` does not exist.

- [ ] **Step 3: Implement the minimal fork**

First add `ContextWindowTracker.fork_for_dispatch()` that constructs a tracker with a
deep copy of settings, a shallow copy of the strategy (preserving safe provider/client
references), and fresh counters. Then use `copy.copy(self)` for immutable/heavy runner
references and explicitly rebuild mutable state:

```python
def fork_for_dispatch(self) -> AgentRunner:
    fork = copy.copy(self)
    fork.config = self.config.model_copy(deep=True)
    fork.tool_bridge = ToolAttachmentBridge.from_config(fork.config.tool_attachments)
    fork._mcp_manager = None
    fork.context_tracker = (
        self.context_tracker.fork_for_dispatch()
        if self.context_tracker is not None
        else None
    )
    return fork
```

Preserve caller-supplied provider, memory resolver, budget enforcer, tool executor, thread store, and sanitizer references as starting dependencies; they are references on the fork and any later reassignment is local.

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/agent_runtime/test_runner_dispatch_isolation.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zeroth/core/agent_runtime/runner.py src/zeroth/core/context_window/tracker.py tests/agent_runtime/test_runner_dispatch_isolation.py
git commit -m "fix: add dispatch-local agent runners"
```

### Task 2: Use the fork in orchestrator dispatch

**Files:**
- Modify: `src/zeroth/core/orchestrator/runtime.py:1257`
- Test: `tests/orchestrator/test_concurrent_agent_dispatch_isolation.py`

- [ ] **Step 1: Write a deterministic interleaving regression test**

Create a provider whose first two calls wait on a barrier and record the runner config,
provider wrapper, tenant, memory resolver result, budget-enforcer tenant, context tracker,
and tool executor visible during each call. Launch two `_dispatch_node` calls with
different runs, tenants, rendered instructions, memory data, budgets, and tool results
using `asyncio.gather`.

```python
assert seen["run-a"].instruction == "tenant-a prompt"
assert seen["run-b"].instruction == "tenant-b prompt"
assert seen["run-a"].tenant_id == "tenant-a"
assert seen["run-b"].tenant_id == "tenant-b"
assert seen["run-a"].memory_value == "memory-a"
assert seen["run-b"].memory_value == "memory-b"
assert seen["run-a"].budget_tenant == "tenant-a"
assert seen["run-b"].budget_tenant == "tenant-b"
assert seen["run-a"].tool_value == "tool-a"
assert seen["run-b"].tool_value == "tool-b"
assert seen["run-a"].context_tracker is not seen["run-b"].context_tracker
assert prototype.config.instruction == "base"
assert prototype.provider is base_provider
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/orchestrator/test_concurrent_agent_dispatch_isolation.py`
Expected: FAIL because both dispatches mutate the registered runner.

- [ ] **Step 3: Fork immediately after prototype lookup**

```python
prototype = self.agent_runners.get(node.node_id) or self.agent_runners.get(base_node_id(node.node_id))
if prototype is None:
    raise NodeDispatcherError(f"no agent runner registered for {node.node_id}")
runner = prototype.fork_for_dispatch() if hasattr(prototype, "fork_for_dispatch") else prototype
```

The fallback is only for lightweight test doubles. Remove restoration assignments whose only purpose was protecting the prototype; keep resource cleanup and support test doubles without the fork method.

- [ ] **Step 4: Run GREEN and adjacent tests**

Run: `uv run pytest -q tests/orchestrator/test_concurrent_agent_dispatch_isolation.py tests/orchestrator/test_tool_edge_dispatch.py tests/context_window/test_orchestrator_integration.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zeroth/core/orchestrator/runtime.py tests/orchestrator/test_concurrent_agent_dispatch_isolation.py
git commit -m "fix: isolate concurrent agent dispatch state"
```

### Task 3: Verify cleanup and prototype immutability

**Files:**
- Modify: `tests/orchestrator/test_concurrent_agent_dispatch_isolation.py`
- Modify: `tests/agent_runtime/test_runner_dispatch_isolation.py`

- [ ] Add failure-path tests proving a raised provider/tool/MCP error leaves the prototype untouched and closes only the fork's MCP manager.
- [ ] Run: `uv run pytest -q tests/agent_runtime/test_runner_dispatch_isolation.py tests/orchestrator/test_concurrent_agent_dispatch_isolation.py`.
- [ ] Run: `uv run pytest -q tests/agent_runtime tests/orchestrator`.
- [ ] Commit tests/refactor only after all remain green.
