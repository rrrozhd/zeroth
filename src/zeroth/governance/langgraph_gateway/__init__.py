"""Governance-owned LangGraph gateway surfaces.

The gateway's capability reporting and its audit event sink are governance
concerns rather than request-time service machinery, so they live here (ZER-24,
Phase B). The legacy paths under :mod:`zeroth.contracts.langgraph_gateway` republish
these objects lazily for compatibility; import from here instead, and see
docs/backend-import-migration.md.
"""
