"""Probes for the gaps the capture boundary closed, one per finding.

Each test here places a seeded value somewhere an earlier version of the
boundary let it through, so each fails against that earlier behaviour rather
than merely restating the current one:

* two mapping entries whose keys both canonicalize unsafely -- rendering keys
  to a single marker made them collide, so a two-entry payload was persisted as
  a one-entry schema and a count of one, and R4's retained counts were wrong;
* a registered secret nested inside a ``set`` -- the container type none of the
  three stacked walkers traversed, so it reached the durable JSON of a
  content-mode record intact;
* a lower-case credential filed under a label key -- the case that showed a
  shape check is not a provenance check, because a token is punctuated exactly
  like a reason code;
* an artifact reference's ``store``, ``content_type`` and nested ``metadata`` --
  the fields that rode into a metadata-only capture alongside the one key
  retention actually needs.
"""

from __future__ import annotations

import json

from zeroth.governance.audit.capture_artifacts import ARTIFACT_KEYS_FIELD
from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    AuditCapturePolicy,
    CaptureDecision,
)
from zeroth.governance.audit.capture_projection import ENTRIES_KEY, ContentFreeProjection
from zeroth.governance.audit.capture_scrub import RedactionChain
from zeroth.governance.audit.models import NodeAuditRecord

SECRET = "sk-proj-SEEDED-CAPTURE-PROBE-9f31c7"
# Bounded, lower-case, punctuated exactly the way ``runtime_not_allowed`` is.
LABEL_SHAPED_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz012345"
RUN_ID = "run-1"
NODE_ID = "node-1"
ARTIFACT_KEY = f"{RUN_ID}/{NODE_ID}/{'a' * 32}"


class _UnprintableKey:
    """A mapping key the projection refuses to print, carrying a secret in its text."""

    def __str__(self) -> str:
        return SECRET

    def __repr__(self) -> str:
        return SECRET

    def __hash__(self) -> int:
        return hash(id(self))

    def __eq__(self, other: object) -> bool:
        return self is other


class _ContentClassifier:
    """The explicit opt-in: this event may retain content."""

    def classify(self, record: NodeAuditRecord) -> str:
        """Answer ``content`` whatever the record holds."""
        del record
        return CaptureDecision.CONTENT.value


def _record(**overrides: object) -> NodeAuditRecord:
    fields: dict[str, object] = {
        "audit_id": "audit-probe",
        "run_id": RUN_ID,
        "node_id": NODE_ID,
        "graph_version_ref": "graph:v1",
        "deployment_ref": "deployment-1",
        "tenant_id": "tenant-a",
        "status": "completed",
    }
    fields.update(overrides)
    return NodeAuditRecord(**fields)  # type: ignore[arg-type]


def _captured(**overrides: object) -> NodeAuditRecord:
    return AuditCapturePolicy().apply(_record(**overrides))


def test_two_entries_with_unprintable_keys_are_retained_as_two_not_collapsed_into_one() -> None:
    # The collision: both keys render to the same marker, so as *dict keys* the
    # second overwrote the first. The submitted payload had two entries and the
    # persisted evidence claimed one -- an under-reported count and a schema
    # missing a branch, which is R4's retained counts being wrong. Entries are a
    # sequence now, so an entry cannot be overwritten by a sibling whatever its
    # key rendered to.
    projection = ContentFreeProjection(RedactionChain().scrub)

    summary = projection.summarize({_UnprintableKey(): 1, _UnprintableKey(): "two"})

    assert summary["count"] == 2
    assert sorted(summary["schema"][ENTRIES_KEY]) == [
        ["<key:other>", "int"],
        ["<key:other>", "str"],
    ]
    assert SECRET not in json.dumps(summary)


def test_two_entries_with_unprintable_keys_digest_differently_from_one_of_them() -> None:
    # The digest half of the same collision: collapsing the two entries made the
    # two-entry payload hash identically to the one-entry payload, so the hash
    # that stands in for dropped content could no longer tell them apart.
    projection = ContentFreeProjection(RedactionChain().scrub)

    one = projection.summarize({_UnprintableKey(): 1})
    two = projection.summarize({_UnprintableKey(): 1, _UnprintableKey(): "two"})

    assert one["sha256"] != two["sha256"]


def test_a_registered_secret_nested_in_a_set_is_masked_in_content_mode() -> None:
    # The traversal gap: the key sanitizer, the secret redactor and the PII
    # filter disagreed about what a container is, and none of the three walked a
    # ``set``. Pydantic then serialized the set into the durable JSON of a
    # content-mode record -- the one branch the channel drop does not cover -- so
    # a *registered* secret survived the whole chain untouched.
    policy = AuditCapturePolicy(
        classifier=_ContentClassifier(), known_secrets={"vault_ref": SECRET}
    )

    captured = policy.apply(_record(input_snapshot={"outer": {SECRET, "harmless"}}))

    assert SECRET not in captured.model_dump_json()


def test_a_registered_secret_nested_in_a_frozenset_is_masked_in_content_mode() -> None:
    policy = AuditCapturePolicy(
        classifier=_ContentClassifier(), known_secrets={"vault_ref": SECRET}
    )

    captured = policy.apply(_record(input_snapshot={"outer": [frozenset({SECRET})]}))

    assert SECRET not in captured.model_dump_json()


def test_a_label_shaped_credential_under_a_label_key_is_summarized_not_retained() -> None:
    # A shape check is not a provenance check. ``ghp_...`` is bounded,
    # lower-case and punctuated exactly like ``runtime_not_allowed``, so the
    # "looks structural" gate that used to guard label keys persisted it
    # verbatim under the *default* policy. Text is kept now only when it is a
    # member of that key's closed vocabulary, which no regex can be talked into.
    captured = _captured(
        execution_metadata={
            "reason_code": LABEL_SHAPED_TOKEN,
            "decision": LABEL_SHAPED_TOKEN,
            "status": LABEL_SHAPED_TOKEN,
        }
    )

    assert LABEL_SHAPED_TOKEN not in captured.model_dump_json()
    metadata = captured.execution_metadata
    for key in ("reason_code", "decision", "status"):
        assert set(metadata[key]) == {"sha256", "schema", "count"}


def test_a_reason_code_this_codebase_mints_still_survives_the_same_gate() -> None:
    # The gate is a vocabulary, not a scrub: a registered code stays readable,
    # or an auditor learns that something was refused without learning why.
    captured = _captured(execution_metadata={"reason_code": "runtime_not_allowed"})

    assert captured.execution_metadata["reason_code"] == "runtime_not_allowed"


def test_an_artifact_reference_keeps_only_its_key_never_the_fields_around_it() -> None:
    # Retention harvests artifact keys straight out of the persisted record, so
    # the capture stage has to keep the addressing. It used to keep the whole
    # reference: ``store``, ``content_type``, ``size`` and a free-form nested
    # ``metadata`` all rode into a metadata-only capture, and a seeded secret in
    # any of them was persisted verbatim. Only the key string survives now.
    captured = _captured(
        output_snapshot={
            "artifact": {
                "store": f"s3://bucket?token={SECRET}",
                "key": ARTIFACT_KEY,
                "content_type": f"application/json; boundary={SECRET}",
                "size": 12,
                "metadata": {"vault": SECRET},
            }
        }
    )

    assert SECRET not in captured.model_dump_json()
    assert captured.execution_metadata[CAPTURE_METADATA_KEY][ARTIFACT_KEYS_FIELD] == [ARTIFACT_KEY]


def test_generated_artifact_for_slash_bearing_run_is_retained() -> None:
    from zeroth.platform.artifacts.models import generate_artifact_key

    run_id = "slash/run"
    key = generate_artifact_key(run_id, NODE_ID)
    captured = _captured(
        run_id=run_id,
        output_snapshot={
            "artifact": {
                "store": "filesystem",
                "key": key,
                "content_type": "application/octet-stream",
                "size": 3,
            }
        },
    )

    assert captured.execution_metadata[CAPTURE_METADATA_KEY][ARTIFACT_KEYS_FIELD] == [key]


def test_an_artifact_key_a_producer_merely_prefixed_is_not_retained() -> None:
    # The prefix check a producer satisfies by prefixing its own string: the key
    # has to match the whole minted grammar, run id and node id and uuid4 hex.
    captured = _captured(
        output_snapshot={
            "artifact": {
                "store": "s3://bucket",
                "key": f"{RUN_ID}/{SECRET}",
                "content_type": "application/json",
                "size": 12,
            }
        }
    )

    assert SECRET not in captured.model_dump_json()
    assert ARTIFACT_KEYS_FIELD not in captured.execution_metadata[CAPTURE_METADATA_KEY]
