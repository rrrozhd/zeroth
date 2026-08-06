"""Contract-owned LangGraph gateway data shapes.

The gateway's wire models and its endpoint inventory are pure data plus
classification rules, so they belong in ``contracts`` rather than in the
service package the rest of the gateway lives in (ZER-24, Phase B). The legacy
paths under :mod:`zeroth.contracts.langgraph_gateway` republished these objects lazily
until they were removed in 0.17
for compatibility; import from here instead, and see
docs/backend-import-migration.md.
"""
