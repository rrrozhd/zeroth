# Check target API

Configure one importable `module:build_target` function:

```python
def build_target(bindings: CheckBindings) -> LangGraphCheckTarget:
    selected = bindings.tool("charge", charge, "side_effecting")
    repository = bindings.action_repository
    # Wrap selected at the candidate-owned governed boundary using repository.
    return LangGraphCheckTarget(...)
```

`bindings.tool` has no default side-effect class: use exactly `read_only` or `side_effecting`.
Duplicate/blank names and unstable schemas are invalid. Record mode returns the live callable.
Replay and fault modes extract only schema metadata, replace the callable synchronously, and do
not retain a live reference.

The target supplies a graph factory, a durable checkpointer factory that accepts the harness path,
a stable case-input builder, and an invocation-config builder. It must rebuild from a fresh process
without physical worker/attempt IDs entering either logical input or action identity. A full Check
also requires the target to request and wire the exact `bindings.action_repository` instance.

Runtime flow:

```text
config -> fresh build_target -> registered metadata -> taped tools
       -> compiled graph -> action repository/checkpointer -> evidence -> verdict
```

Debug target import/digest failures in `zeroth.check.adapter.loading`; schema substitution in
`adapter.bindings` and `replay.tools`; durable action behavior in the LangGraph action lifecycle.
