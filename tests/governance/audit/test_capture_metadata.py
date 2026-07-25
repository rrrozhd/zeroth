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

import hashlib
import logging

import pytest

from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    AuditCapturePolicy,
    CaptureDecision,
)
from zeroth.governance.audit.capture_projection import (
    ALLOWED_METADATA_KEYS,
    canonicalize,
    key_digest,
)
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
    # ``reviewer`` is chosen outside this codebase, so it survives as a digest:
    # the record still correlates two approvals by the same reviewer, and a
    # credential typed into that field is not persisted as text either.
    assert set(action.metadata) == {"reviewer"}
    assert action.metadata["reviewer"]["sha256"] == hashlib.sha256(b'"ops"').hexdigest()
    assert SECRET not in captured.model_dump_json()


def test_a_mapping_key_that_renders_as_a_secret_never_reaches_the_persisted_schema() -> None:
    # R5: the key's __str__ authored the schema entry, so the secret was
    # persisted as a "shape". Keys are gated by type now, never printed.
    captured = _captured(input_snapshot={"outer": {_LeakingKey(): "value"}})

    assert SECRET not in captured.model_dump_json()
    schema = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]["input_snapshot"][
        "schema"
    ]
    assert schema == {key_digest("outer"): {key_digest("***REDACTED***"): "str"}}


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


ROOT_KEY = "AKIAIOSFODNN7EXAMPLE"


class _RegisteredSecretKey:
    """A non-string mapping key whose text is a registered secret."""

    def __str__(self) -> str:
        return SECRET

    def __hash__(self) -> int:
        return 11

    def __eq__(self, other: object) -> bool:
        return self is other


class _ContentClassifier:
    """The explicit opt-in: this event may retain content."""

    def classify(self, record: NodeAuditRecord) -> str:
        """Answer ``content`` whatever the record holds."""
        del record
        return CaptureDecision.CONTENT.value


def test_a_credential_in_the_client_supplied_correlation_id_is_not_persisted() -> None:
    # The probe: ``correlation_id`` is filled straight from the gateway's
    # ``X-Correlation-ID`` request header, and every allowlisted key used to
    # accept any short string, so a credential pasted into that header was
    # persisted verbatim under the default policy.
    captured = _captured(execution_metadata={"correlation_id": ROOT_KEY})

    assert ROOT_KEY not in captured.model_dump_json()
    summary = captured.execution_metadata["correlation_id"]
    assert set(summary) == {"sha256", "schema", "count"}


@pytest.mark.parametrize("key", sorted(ALLOWED_METADATA_KEYS))
def test_a_seeded_credential_in_any_allowlisted_field_is_absent_from_the_record(
    key: str,
) -> None:
    # Every allowlisted key, not just the one the probe happened to name: a key
    # is allowlisted for the *kind* of value it carries, and a credential is not
    # that kind whatever the key is called.
    captured = _captured(execution_metadata={key: ROOT_KEY})

    assert ROOT_KEY not in captured.model_dump_json()


@pytest.mark.parametrize("key", sorted(ALLOWED_METADATA_KEYS))
def test_a_seeded_api_key_in_any_allowlisted_field_is_absent_from_the_record(
    key: str,
) -> None:
    captured = _captured(execution_metadata={key: SECRET})

    assert SECRET not in captured.model_dump_json()


def test_the_structural_values_an_allowlisted_key_is_for_still_survive() -> None:
    # The other half: a typed projection that dropped everything would be a
    # scrub, not an allowlist. Numbers, booleans, digests and lower-case
    # vocabulary terms are exactly what these keys exist to carry.
    captured = _captured(
        execution_metadata={
            "node_kind": "agent",
            "operation": "threads.create",
            "duration_ms": 42.5,
            "upstream_status_code": 200,
            "budget_check_degraded": False,
            "input_sha256": "a" * 64,
            "compatibility_fingerprint": "sha256:" + "b" * 64,
        }
    )

    metadata = captured.execution_metadata
    assert metadata["node_kind"] == "agent"
    assert metadata["operation"] == "threads.create"
    assert metadata["duration_ms"] == 42.5
    assert metadata["upstream_status_code"] == 200
    assert metadata["budget_check_degraded"] is False
    assert metadata["input_sha256"] == "a" * 64
    assert metadata["compatibility_fingerprint"] == "sha256:" + "b" * 64


def test_a_value_of_the_wrong_kind_for_its_key_is_summarized_rather_than_kept() -> None:
    captured = _captured(execution_metadata={"duration_ms": ROOT_KEY, "input_sha256": ROOT_KEY})

    assert ROOT_KEY not in captured.model_dump_json()
    assert set(captured.execution_metadata["duration_ms"]) == {"sha256", "schema", "count"}
    assert set(captured.execution_metadata["input_sha256"]) == {"sha256", "schema", "count"}


def test_an_identifier_shaped_credential_used_as_a_mapping_key_never_reaches_the_schema() -> None:
    # The probe: ``AKIAIOSFODNN7EXAMPLE`` passes every "looks like a name" test
    # there is, so gating schema keys on shape persisted it verbatim inside the
    # dropped-content summary. Keys are hashed now, never printed.
    captured = _captured(input_snapshot={ROOT_KEY: "value"})

    assert ROOT_KEY not in captured.model_dump_json()
    schema = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]["input_snapshot"][
        "schema"
    ]
    assert schema == {key_digest(ROOT_KEY): "str"}


def test_a_dynamically_named_type_cannot_author_a_schema_entry() -> None:
    # A schema only ever describes canonical values, so its type names come from
    # a closed set; a class carrying a credential as its ``__name__`` cannot
    # smuggle it in as "the type this value had".
    sneaky = type(SECRET, (), {})

    captured = _captured(input_snapshot={"outer": sneaky()})

    assert SECRET not in captured.model_dump_json()


def test_a_non_string_key_cannot_persist_a_registered_secret_in_content_mode() -> None:
    # The content branch is the one place the channel drop does not cover, and
    # every rung of the redaction chain walked values only: the sanitizer turned
    # the key into ``str(key)`` and the secret redactor never looked at keys, so
    # a registered secret used as a mapping key was reproduced verbatim.
    policy = AuditCapturePolicy(
        classifier=_ContentClassifier(), known_secrets={"vault_ref": SECRET}
    )

    captured = policy.apply(_record(input_snapshot={"outer": {_RegisteredSecretKey(): "v"}}))

    assert SECRET not in captured.model_dump_json()


def test_a_registered_secret_used_as_a_string_key_is_masked_in_content_mode() -> None:
    policy = AuditCapturePolicy(
        classifier=_ContentClassifier(), known_secrets={"vault_ref": SECRET}
    )

    captured = policy.apply(_record(input_snapshot={SECRET: "value"}))

    assert SECRET not in captured.model_dump_json()
