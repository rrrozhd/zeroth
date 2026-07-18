"""Contract-owned graph validators.

Each module here checks one concern of a graph and appends
``ValidationIssue`` values to a caller-supplied list. They are deliberately
free of runtime, governance, and integration dependencies so the contracts
layer can own them.

Composition lives outside this package: the public ``GraphValidator`` also
runs execution-level checks (parallel config, capability grants) that need
the runtime and governance layers, so it is assembled there.
"""
