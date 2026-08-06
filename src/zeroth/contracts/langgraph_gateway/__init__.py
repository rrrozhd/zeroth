"""Contract-owned LangGraph gateway data shapes.

The gateway's wire models and its endpoint inventory are pure data plus
classification rules, so they belong in ``contracts`` rather than in the
service package the rest of the gateway lives in (ZER-24, Phase B). The legacy
paths under :mod:`zeroth.core.langgraph_gateway` republish these objects lazily
for compatibility; import from here instead, and see
docs/backend-import-migration.md.
"""
