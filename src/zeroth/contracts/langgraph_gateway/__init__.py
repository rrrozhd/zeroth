"""Contract-owned LangGraph gateway data shapes.

The gateway's wire models and its endpoint inventory are pure data plus
classification rules, so they belong in ``contracts`` rather than in the
service package the rest of the gateway lives in (ZER-24, Phase B).

The gateway is split across three domains by what each part is: data shapes
here, capability and event surfaces in
:mod:`zeroth.governance.langgraph_gateway`, and request-time machinery in
:mod:`zeroth.service.langgraph_gateway`.
"""
