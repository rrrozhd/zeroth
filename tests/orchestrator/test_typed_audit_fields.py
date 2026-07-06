"""Typed audit-field promotion.

``NodeAuditRecord.tool_calls`` / ``.memory_interactions`` used to stay empty —
the data only lived in ``execution_metadata.extra``. ``_typed_audit_fields``
promotes it into the typed (queryable, evidence-summarized) fields. These tests
cover the mapping and, crucially, tolerance of redacted shapes: a redacted
argument must still produce a record, not silently drop the call (which would
re-empty the field on exactly the records carrying sensitive data).
"""

from __future__ import annotations

from zeroth.core.orchestrator.runtime import RuntimeOrchestrator

_fields = RuntimeOrchestrator._typed_audit_fields


def test_missing_or_empty_extra_returns_empty() -> None:
    assert _fields({}) == ([], [])
    assert _fields({"extra": None}) == ([], [])
    assert _fields({"extra": {}}) == ([], [])


def test_memory_interactions_round_trip() -> None:
    record = {
        "extra": {
            "memory_interactions": [
                {
                    "memory_ref": "memory://conversation",
                    "connector_type": "thread",
                    "scope": "thread",
                    "operation": "read",
                    "key": "latest",
                    "value": None,
                },
                {
                    "memory_ref": "memory://conversation",
                    "connector_type": "thread",
                    "scope": "thread",
                    "operation": "write",
                    "key": "latest",
                    "value": {"answer": "Paris"},
                },
            ]
        }
    }
    _, memory = _fields(record)
    assert [m.operation for m in memory] == ["read", "write"]
    assert memory[0].memory_ref == "memory://conversation"
    assert memory[1].value == {"answer": "Paris"}


def test_tool_calls_mapped_from_nested_tool() -> None:
    record = {
        "extra": {
            "tool_calls": [
                {
                    "tool": {
                        "alias": "format_article",
                        "executable_unit_ref": "eu://format_article",
                    },
                    "arguments": {"topic": "t", "body": "b"},
                    "outcome": {"formatted": "# T"},
                    "error": None,
                }
            ]
        }
    }
    tools, _ = _fields(record)
    assert len(tools) == 1
    tc = tools[0]
    assert tc.tool_ref == "eu://format_article"
    assert tc.alias == "format_article"
    assert tc.arguments == {"topic": "t", "body": "b"}
    assert tc.outcome == {"formatted": "# T"}
    assert tc.error is None


def test_tolerant_of_redacted_argument_shapes() -> None:
    # If redaction (or anything else) hands back a non-dict arguments/outcome,
    # coerce it into a dict so the call is still recorded rather than dropped.
    record = {
        "extra": {
            "tool_calls": [
                {
                    "tool": {"alias": "x", "executable_unit_ref": "eu://x"},
                    "arguments": "[REDACTED]",
                    "outcome": None,
                    "error": "boom",
                }
            ]
        }
    }
    tools, _ = _fields(record)
    assert len(tools) == 1
    assert tools[0].arguments == {"redacted": "[REDACTED]"}
    assert tools[0].outcome is None
    assert tools[0].error == "boom"


def test_non_str_error_is_coerced_not_dropped() -> None:
    # A malformed (non-string) error must not raise out and abort the audit
    # write; coerce it to a string and keep the call.
    record = {
        "extra": {
            "tool_calls": [
                {
                    "tool": {"alias": "x", "executable_unit_ref": "eu://x"},
                    "arguments": {"a": 1},
                    "error": {"code": 500},
                }
            ]
        }
    }
    tools, _ = _fields(record)
    assert len(tools) == 1
    assert tools[0].error == "{'code': 500}"


def test_redaction_before_mapping_removes_secrets_from_typed_fields() -> None:
    # The completed/failed/branch audit sites redact the record BEFORE mapping it
    # into typed columns. Prove a resolved secret in tool args/outcome + memory
    # value does not survive redact-then-map (the invariant the branch fix relies on).
    from zeroth.core.secrets.redaction import SecretRedactor

    secret = "sk-SUPERSECRET"  # noqa: S105 — test fixture
    record = {
        "extra": {
            "tool_calls": [
                {
                    "tool": {"alias": "x", "executable_unit_ref": "eu://x"},
                    "arguments": {"api_key": secret},
                    "outcome": {"echo": secret},
                    "error": None,
                }
            ],
            "memory_interactions": [
                {
                    "memory_ref": "m://c",
                    "connector_type": "thread",
                    "scope": "thread",
                    "operation": "write",
                    "key": "latest",
                    "value": {"token": secret},
                }
            ],
        }
    }
    redacted = SecretRedactor({"secret/x": secret}).redact(dict(record))
    tools, mem = _fields(redacted)
    assert secret not in str(tools[0].arguments)
    assert secret not in str(tools[0].outcome)
    assert secret not in str(mem[0].value)


def test_redacted_leaf_inside_dict_argument_preserved() -> None:
    # The common production shape: structure preserved, a leaf value redacted.
    record = {
        "extra": {
            "tool_calls": [
                {
                    "tool": {"alias": "x", "executable_unit_ref": "eu://x"},
                    "arguments": {"api_key": "[REDACTED:secret/demo]", "n": 1},
                    "outcome": {"ok": True},
                }
            ]
        }
    }
    tools, _ = _fields(record)
    assert tools[0].arguments == {"api_key": "[REDACTED:secret/demo]", "n": 1}
