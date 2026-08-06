"""Service-owned LangGraph gateway machinery.

Admission, compatibility detection, reserved-context handling, header policy,
transport, the enforcement service and its store are all request-time concerns
the gateway service owns (ZER-24, Phase B). The legacy paths under
:mod:`zeroth.contracts.langgraph_gateway` republish these objects lazily for
compatibility; import from here instead, and see
docs/backend-import-migration.md.
"""
