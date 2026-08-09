"""Ordered, fail-closed release gates bound to the exact candidate.

``release/langgraph`` stays the LangGraph slice's own evidence harness and is
invoked by file path from CI. This package is the gate substrate the rest of
the release pipeline validates against: a manifest of gates, candidate-derived
identity, fail-closed validation, and a human-readable verdict.
"""

from __future__ import annotations

SCHEMA_VERSION = 1
