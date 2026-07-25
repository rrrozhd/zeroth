"""What a metadata-only capture is allowed to keep, and how it describes the rest.

R4 says content capture is off by default and that hashes, schemas, counts,
timing, outcomes and decision metadata survive. The channels these tests attack
are the ones that used to slip through that promise because they were merely
*scrubbed* rather than projected: ``execution_metadata`` and approval metadata
are free-form ``dict[str, Any]`` a producer fills with whatever it was holding,
and ``error`` fields are free-form text that an exception can author.

Each probe below places a seeded secret somewhere the old best-effort scrub did
not look -- under an unexpected metadata key, inside a tuple, in a record or
tool error, in an approval note, and inside a mapping key whose ``__str__``
returns it -- and asserts it is absent from the serialized record. The last one
also pins R4's hash guarantee: the same payload used to make the digest
``None``, quietly removing the evidence that was supposed to stand in for the
dropped content.
"""

from __future__ import annotations

import logging

import pytest

from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    AuditCapturePolicy,
    CaptureDecision,
)
from zeroth.governance.audit.capture_projection import ALLOWED_METADATA_KEYS, canonicalize
from zeroth.governance.audit.models import (
    ApprovalActionRecord,
    NodeAuditRecord,
    ToolCallRecord,
)

SECRET = "sk-proj-SEEDED-CAPTURE-PROBE-9f31c7"


class _LeakingKey:
    """A mapping key that renders as a secret -- the schema-authoring probe."""

    def __str__(self) -> str:
        return SECRET

    def __repr__(self) -> str:
        return SECRET

    def __hash__(self) -> int:
        return 7

    def __eq__(self, other: object) -> bool:
        return self is other


class _LeakingClassifier:
    """A classifier whose failure message carries the value it was inspecting."""

    def classify(self, record: NodeAuditRecord) -> str:
        del record
        raise RuntimeError(f"classification failed for {SECRET}")


def _record(**overrides: object) -> NodeAuditRecord:
    fields: dict[str, object] = {
        "audit_id": "audit-probe",
        "run_id": "run-1",
        "node_id": "node-1",
        "graph_version_ref": "graph:v1",
        "deployment_ref": "deployment-1",
        "tenant_id": "tenant-a",
        "status": "completed",
    }
    fields.update(overrides)
    return NodeAuditRecord(**fields)  # type: ignore[arg-type]


def _captured(**overrides: object) -> NodeAuditRecord:
    return AuditCapturePolicy().apply(_record(**overrides))


def test_an_unrecognised_metadata_key_is_dropped_rather_than_scrubbed() -> None:
    # The probe: a prompt filed under execution_metadata["prompt"] survived key
    # redaction untouched, because no key rule was ever going to name "prompt".
    captured = _captured(execution_metadata={"prompt": SECRET, "node_kind": "agent"})

    assert "prompt" not in captured.execution_metadata
    assert captured.execution_metadata["node_kind"] == "agent"
    assert SECRET not in captured.model_dump_json()


def test_a_container_under_an_allowlisted_key_is_summarized_not_retained() -> None:
    # A tuple is not a bounded scalar, and its contents are not metadata.
    captured = _captured(execution_metadata={"operation": ("credential", SECRET)})

    summary = captured.execution_metadata["operation"]
    assert set(summary) == {"sha256", "schema", "count"}
    assert len(summary["sha256"]) == 64
    assert SECRET not in captured.model_dump_json()


def test_every_dropped_metadata_key_is_still_counted_and_digested() -> None:
    # Dropping is not forgetting: the whole submitted mapping keeps a digest, a
    # shape and a tally of what the allowlist refused.
    captured = _captured(execution_metadata={"prompt": SECRET, "secret_notes": SECRET})

    summary = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"][
        "execution_metadata"
    ]
    assert summary["dropped_keys"] == 2
    assert len(summary["sha256"]) == 64
    assert summary["count"] == 2


def test_record_error_text_is_replaced_by_a_bounded_marker_and_a_digest() -> None:
    # An exception message carries whatever the raising code was holding.
    captured = _captured(error=f"connection to vault failed: {SECRET}")

    assert captured.error == "***REDACTED***"
    summary = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]["error"]
    assert len(summary["sha256"]) == 64
    assert SECRET not in captured.model_dump_json()


def test_a_record_without_an_error_does_not_gain_one() -> None:
    assert _captured().error is None


def test_tool_call_error_text_is_dropped_while_the_tool_identity_survives() -> None:
    captured = _captured(
        tool_calls=[ToolCallRecord(tool_ref="tool:http", alias="http", error=f"401 using {SECRET}")]
    )

    [call] = captured.tool_calls
    assert call.tool_ref == "tool:http"
    assert call.error == "***REDACTED***"
    assert SECRET not in captured.model_dump_json()


def test_approval_metadata_is_projected_onto_the_allowlist() -> None:
    # Approval actions are decision evidence, so the decision survives; the
    # free-form note a reviewer typed into it does not.
    captured = _captured(
        approval_actions=[
            ApprovalActionRecord(
                approval_id="approval-1",
                action="approved",
                metadata={"reviewer": "ops", "note": f"used {SECRET}"},
            )
        ]
    )

    [action] = captured.approval_actions
    assert action.action == "approved"
    assert action.metadata == {"reviewer": "ops"}
    assert SECRET not in captured.model_dump_json()


def test_a_mapping_key_that_renders_as_a_secret_never_reaches_the_persisted_schema() -> None:
    # R5: the key's __str__ authored the schema entry, so the secret was
    # persisted as a "shape". Keys are gated by type now, never printed.
    captured = _captured(input_snapshot={"outer": {_LeakingKey(): "value"}})

    assert SECRET not in captured.model_dump_json()
    schema = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]["input_snapshot"][
        "schema"
    ]
    assert schema == {"outer": {"***REDACTED***": "str"}}


def test_a_payload_with_an_unrenderable_key_still_produces_a_digest() -> None:
    # R4's hash guarantee: the same payload made json.dumps raise, so the digest
    # came back None and the evidence replacing the dropped content vanished.
    captured = _captured(input_snapshot={"outer": {_LeakingKey(): "value"}})

    summary = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]["input_snapshot"]
    assert len(summary["sha256"]) == 64


def test_a_string_subclass_key_is_canonicalized_without_calling_its_own_str() -> None:
    class _Sneaky(str):
        def __str__(self) -> str:
            return SECRET

    assert canonicalize({_Sneaky("plain"): _Sneaky("value")}) == {"***REDACTED***": "value"}


def test_the_metadata_allowlist_is_a_closed_set_of_structural_keys() -> None:
    # A guard on the allowlist itself: content-shaped names must never be added.
    assert "prompt" not in ALLOWED_METADATA_KEYS
    assert "input_snapshot" not in ALLOWED_METADATA_KEYS
    assert "node_kind" in ALLOWED_METADATA_KEYS


def test_an_explicit_content_classification_keeps_metadata_the_allowlist_would_drop() -> None:
    # The allowlist is the metadata-only posture, not a second content filter:
    # an event explicitly classified into content still carries its metadata.
    class _Content:
        def classify(self, record: NodeAuditRecord) -> str:
            del record
            return CaptureDecision.CONTENT.value

    captured = AuditCapturePolicy(classifier=_Content()).apply(
        _record(execution_metadata={"prompt": "hello"})
    )

    assert captured.execution_metadata["prompt"] == "hello"


def test_a_classifier_failure_is_logged_as_a_code_and_type_never_as_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The log stream is an export path none of the record-level checks cover, so
    # str(exc) from a classifier put the seeded secret straight into it.
    with caplog.at_level(logging.WARNING, logger="zeroth.governance.audit.capture_policy"):
        captured = AuditCapturePolicy(classifier=_LeakingClassifier()).apply(_record())

    assert captured.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is False
    emitted = " ".join(record.getMessage() for record in caplog.records)
    assert SECRET not in emitted
    assert "classifier_failed" in emitted
    assert "RuntimeError" in emitted


def test_a_capture_failure_is_logged_as_a_code_and_type_never_as_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _LeakingPolicy(AuditCapturePolicy):
        def _apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
            del record
            raise RuntimeError(f"walker failed on {SECRET}")

    with caplog.at_level(logging.WARNING, logger="zeroth.governance.audit.capture_policy"):
        captured = _LeakingPolicy().apply(_record())

    assert captured.execution_metadata[CAPTURE_METADATA_KEY]["capture_failed"] is True
    emitted = " ".join(record.getMessage() for record in caplog.records)
    assert SECRET not in emitted
    assert "capture_failed" in emitted


def test_a_transform_that_fails_mid_walk_returns_a_blank_record_not_a_raw_one() -> None:
    # The other fail-closed direction: a broken transform loses the content, not
    # the guarantee.
    class _Exploding(AuditCapturePolicy):
        def _apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
            del record
            raise RuntimeError("transform failed")

    blanked = _Exploding().apply(
        _record(
            input_snapshot={"prompt": SECRET},
            stdout=SECRET,
            tool_calls=[ToolCallRecord(tool_ref="tool:http", alias="http", error=SECRET)],
        )
    )

    assert blanked.input_snapshot == {}
    assert blanked.tool_calls == []
    assert blanked.memory_interactions == []
    assert blanked.stdout is None
    assert blanked.execution_metadata[CAPTURE_METADATA_KEY]["capture_failed"] is True
    assert SECRET not in blanked.model_dump_json()
