"""Guard the vendored ``gen_ai.*`` constants against semantic-convention drift.

``_genai.py`` defines the semconv strings itself: OpenTelemetry's GenAI module is
private and incubating (``opentelemetry.semconv._incubating.attributes``) and is
only transitively installed, so importing it in production code would break an
install without the ``otel`` extra. This test closes the loop -- whenever semconv
*is* importable, every vendored value must equal semconv's.

Symbols are compared **per name and only when semconv defines them**. The GenAI
conventions are still incubating, so ``gen_ai.workflow.name``,
``invoke_workflow`` and ``retrieval`` are absent from older releases (they are
missing from ``opentelemetry-semantic-conventions`` 0.58b0, the version resolved
here); a hard reference would fail on install rather than on drift. A separate
test asserts the long-stable subset really is present, so this can never pass
vacuously.
"""

from __future__ import annotations

import pytest

from zeroth.integrations.langgraph._genai import (
    GEN_AI_AGENT_NAME,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_NAME,
    GEN_AI_WORKFLOW_NAME,
    OPERATION_CHAT,
    OPERATION_EMBEDDINGS,
    OPERATION_EXECUTE_TOOL,
    OPERATION_INVOKE_AGENT,
    OPERATION_INVOKE_WORKFLOW,
    OPERATION_RETRIEVAL,
)

semconv = pytest.importorskip("opentelemetry.semconv._incubating.attributes.gen_ai_attributes")

_VENDORED_ATTRIBUTES = {
    "GEN_AI_OPERATION_NAME": GEN_AI_OPERATION_NAME,
    "GEN_AI_TOOL_NAME": GEN_AI_TOOL_NAME,
    "GEN_AI_AGENT_NAME": GEN_AI_AGENT_NAME,
    "GEN_AI_WORKFLOW_NAME": GEN_AI_WORKFLOW_NAME,
    "GEN_AI_CONVERSATION_ID": GEN_AI_CONVERSATION_ID,
}

_VENDORED_OPERATIONS = {
    "CHAT": OPERATION_CHAT,
    "EXECUTE_TOOL": OPERATION_EXECUTE_TOOL,
    "INVOKE_AGENT": OPERATION_INVOKE_AGENT,
    "INVOKE_WORKFLOW": OPERATION_INVOKE_WORKFLOW,
    "RETRIEVAL": OPERATION_RETRIEVAL,
    "EMBEDDINGS": OPERATION_EMBEDDINGS,
}

# Present in every semconv release this project has ever resolved; if one of
# these disappears the comparison has stopped covering anything and must fail.
_ALWAYS_DEFINED_ATTRIBUTES = (
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_TOOL_NAME",
    "GEN_AI_AGENT_NAME",
    "GEN_AI_CONVERSATION_ID",
)
_ALWAYS_DEFINED_OPERATIONS = ("CHAT", "EXECUTE_TOOL", "INVOKE_AGENT", "EMBEDDINGS")


@pytest.mark.parametrize(("symbol", "vendored"), sorted(_VENDORED_ATTRIBUTES.items()))
def test_vendored_attribute_key_matches_semconv(symbol: str, vendored: str) -> None:
    if not hasattr(semconv, symbol):
        pytest.skip(f"semconv does not define {symbol} yet")

    assert getattr(semconv, symbol) == vendored


@pytest.mark.parametrize(("member", "vendored"), sorted(_VENDORED_OPERATIONS.items()))
def test_vendored_operation_value_matches_semconv(member: str, vendored: str) -> None:
    values = semconv.GenAiOperationNameValues
    if not hasattr(values, member):
        pytest.skip(f"semconv does not define GenAiOperationNameValues.{member} yet")

    assert getattr(values, member).value == vendored


def test_the_stable_semconv_symbols_are_actually_present() -> None:
    missing_attributes = [name for name in _ALWAYS_DEFINED_ATTRIBUTES if not hasattr(semconv, name)]
    values = semconv.GenAiOperationNameValues
    missing_operations = [name for name in _ALWAYS_DEFINED_OPERATIONS if not hasattr(values, name)]

    assert not missing_attributes
    assert not missing_operations
