"""Capture classification and redaction applied before the durable audit write.

An audit record is evidence, and the producer that mints one is the wrong place
to decide what may be kept: the producer is holding the prompt, the tool
arguments and the model's answer, and a content channel that is closed only
because every caller remembered to clear it is open the first time one caller
forgets. This module owns that decision instead, and
:class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue` constructs one of
these itself for every event it dequeues.

**The invariant: an event retains content only when it is classified into
content, and anything this stage cannot classify retains none.**
:class:`MetadataOnlyCaptureClassifier` is the default and answers
``metadata_only`` for every event, so a deployment that configures nothing gets
the closed posture. A classifier that raises, or returns a blank string, a
non-``str``, or an unrecognised string, is read the same way: the unknown branch
and the conservative branch are one branch, which is what stops taxonomy drift
from quietly opening the channel.

**Fail-closed twice over.** ``AuditCapturePolicy()`` with no arguments is already
a redacting policy -- the opposite posture from ``RuntimeAuditRecorder.redact``,
which becomes a pass-through when no secret resolver is injected. And
:meth:`AuditCapturePolicy.apply` never raises: it walks producer-supplied
payloads of arbitrary shape, so it can meet a mapping that cycles or a
``__len__`` that throws, and when it does the output is :func:`blank_record` --
a record stripped of every content channel -- never the record that came in.

**Metadata-only is an allowlist, not a scrub.** ``execution_metadata``,
approval metadata and the free-form ``error`` strings are the channels a
producer fills with whatever it happened to be holding, and key-based redaction
over them only masks the key names somebody thought of: a prompt filed under
``execution_metadata["prompt"]``, a password nested inside a tuple, or an
exception message pasted into ``error`` all survived it. Those channels now go
through :class:`~zeroth.governance.audit.capture_projection.ContentFreeProjection`,
which keeps an allowlisted, bounded projection and replaces everything else --
error text included -- with a digest, a schema and a count.

**What is removed and what is kept are equally load-bearing.** Under
``metadata_only`` every content channel (``input_snapshot``, ``output_snapshot``,
``validation_results``, ``condition_results``, ``stdout``, ``stderr``, each tool
call's ``arguments``, ``outcome`` and ``error``, each memory interaction's
``value``, each approval action's unrecognised metadata) is emptied and replaced
by that summary under ``execution_metadata[CAPTURE_METADATA_KEY]``. What makes
the record useful without reproducing content survives untouched: identity and
lineage, ``status``, timing, ``token_usage``, ``cost_usd``, the actor, the
approval decisions themselves and the digest-chain fields. A record whose
evidentiary value died with its content would be a slower way of not auditing.

**The primitives are reused, not reinvented.** Key-based redaction comes from
:class:`~zeroth.governance.audit.sanitizer.PayloadSanitizer`, masking of
registered secret values from
:class:`~zeroth.platform.secrets.redaction.SecretRedactor`, pattern detection
from :class:`~zeroth.governance.guardrails.content.PIIFilter` (``email``,
``ssn`` and ``credit_card`` only -- its phone heuristic matches any ten-digit
run). All three are complements and none carries the guarantee: a key rule
cannot see a secret under an unexpected key, a value rule cannot see a secret
nobody registered, a pattern cannot see what it does not match. The channel drop
and the metadata allowlist are what hold.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from zeroth.governance.audit.capture_projection import (
    MAX_DEPTH,
    REDACTED,
    ContentFreeProjection,
)
from zeroth.governance.audit.models import (
    AuditRedactionConfig,
    MemoryAccessRecord,
    NodeAuditRecord,
    ToolCallRecord,
)
from zeroth.governance.audit.sanitizer import PayloadSanitizer
from zeroth.governance.guardrails.content import PIIFilter
from zeroth.platform.secrets.redaction import SecretRedactor

logger = logging.getLogger(__name__)

CAPTURE_METADATA_KEY = "audit_capture"

# Exact-match keys, because PayloadSanitizer compares key names literally. This
# set is a floor a caller may widen and cannot narrow; it is the complement to
# the channel drop, not the mechanism the guarantee rests on.
DEFAULT_REDACT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

_EMPTIED_MAPPING_FIELDS = ("input_snapshot", "output_snapshot", "validation_results")
_EMPTIED_LIST_FIELDS = ("condition_results",)
_EMPTIED_TEXT_FIELDS = ("stdout", "stderr")


class CaptureDecision(StrEnum):
    """What one classified event allows this stage to retain.

    Two members, and deliberately only two. The classification answers a single
    question -- may content survive? -- and a wider taxonomy would invite a
    caller to return a shade this transform does not implement, which the
    conservative fallback would then silently collapse anyway.
    """

    METADATA_ONLY = "metadata_only"
    CONTENT = "content"


class CaptureClassifier(Protocol):
    """Decide what a single audit event allows to be retained.

    Mirrors the repository's ``classify(payload) -> str`` classifier shape, but
    synchronously: this runs on the delivery worker between the dequeue and the
    durable write, and an awaited classifier would put an unbounded wait in
    front of every audit write. This is the *only* replaceable part of the
    capture boundary: it picks between two fixed outcomes and cannot author
    either, because a security boundary a caller can swap out is not a boundary.
    """

    def classify(self, record: NodeAuditRecord) -> str:
        """Return a :class:`CaptureDecision` value; anything else means metadata-only."""
        ...


class MetadataOnlyCaptureClassifier:
    """The conservative default: no event is ever allowed to retain content."""

    def classify(self, record: NodeAuditRecord) -> str:
        """Classify every event as metadata-only, whatever it holds."""
        del record
        return CaptureDecision.METADATA_ONLY.value


def blank_record(record: NodeAuditRecord) -> NodeAuditRecord:
    """Return a copy of ``record`` carrying identity, lineage, timing and outcome only.

    The last-resort shape, reached when the capture transform itself fails on a
    payload: the only output safe without having successfully inspected the
    input is one that carries none of it. A module-level function rather than a
    policy method so the delivery stage can reach it without holding a working
    policy -- the case where it is needed is the case where the policy did not
    work. ``record`` is never mutated.
    """
    update: dict[str, Any] = dict.fromkeys((*_EMPTIED_TEXT_FIELDS, "error"))
    update |= {name: {} for name in _EMPTIED_MAPPING_FIELDS}
    update |= {name: [] for name in _EMPTIED_LIST_FIELDS}
    update |= {
        "tool_calls": [],
        "memory_interactions": [],
        "approval_actions": [],
        "execution_metadata": {
            CAPTURE_METADATA_KEY: {
                "classification": CaptureDecision.METADATA_ONLY.value,
                "content_retained": False,
                "capture_failed": True,
            }
        },
    }
    return record.model_copy(update=update)


class AuditCapturePolicy:
    """Classify one audit record and strip it to what the classification allows.

    Args:
        classifier: Decides per record whether content may be retained.
        redaction: Extra key-redaction and path-omission rules, merged with
            :data:`DEFAULT_REDACT_KEYS` -- a supplied config widens the default
            and cannot narrow it.
        known_secrets: Resolved secret values to mask wherever they appear.
            Registering none only weakens the value-based complement.
    """

    def __init__(
        self,
        *,
        classifier: CaptureClassifier | None = None,
        redaction: AuditRedactionConfig | None = None,
        known_secrets: Mapping[str, str] | None = None,
    ) -> None:
        self._classifier = MetadataOnlyCaptureClassifier() if classifier is None else classifier
        config = AuditRedactionConfig() if redaction is None else redaction
        self._sanitizer = PayloadSanitizer(
            AuditRedactionConfig(
                redact_keys=set(DEFAULT_REDACT_KEYS) | set(config.redact_keys),
                omit_paths=set(config.omit_paths),
            )
        )
        self._secrets = SecretRedactor(known_secrets)
        self._pii = PIIFilter(("email", "ssn", "credit_card"))
        self._projection = ContentFreeProjection(self._scrub)

    def apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Return the only version of ``record`` this stage is allowed to persist.

        The result is always a fresh ``model_copy``: the submitted record is
        never mutated, and never handed back unchanged.
        """
        try:
            return self._apply(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the payloads walked here are
            # producer-supplied and arbitrarily shaped, so any failure must
            # degrade to the emptiest record rather than propagate to a caller
            # still holding the unredacted original.
            self._log_failure("capture_failed", record.audit_id, exc)
            return blank_record(record)

    def _apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Classify, transform the content channels, and stamp the decision."""
        decision = self._decide(record)
        summaries: dict[str, Any] = {}
        if decision is CaptureDecision.CONTENT:
            update = self._scrubbed_content(record)
            update["error"] = self._scrub(record.error)
        else:
            update, summaries = self._dropped_content(record)
            # Free-form failure text is content: an exception message carries
            # whatever the raising code was holding. The digest and length in
            # ``summaries`` keep the evidence; the field keeps only the fact.
            summaries["error"] = self._projection.summarize(record.error)
            update["error"] = None if record.error is None else REDACTED
        update["execution_metadata"] = self._metadata(record, decision, summaries)
        return record.model_copy(update=update)

    def _decide(self, record: NodeAuditRecord) -> CaptureDecision:
        """Classify one event, reading anything unrecognised as "retain nothing"."""
        try:
            raw = self._classifier.classify(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the classifier is injected, so a
            # deployment-supplied one that throws must land on the conservative
            # decision instead of failing the whole capture.
            self._log_failure("classifier_failed", record.audit_id, exc)
            return CaptureDecision.METADATA_ONLY
        if type(raw) is not str or not raw:
            return CaptureDecision.METADATA_ONLY
        if raw == CaptureDecision.CONTENT.value:
            return CaptureDecision.CONTENT
        return CaptureDecision.METADATA_ONLY

    def _log_failure(self, code: str, audit_id: str, exc: BaseException) -> None:
        """Log a capture failure as a fixed code and an exception type, never a message.

        ``str(exc)`` is attacker-reachable: a classifier or payload walker that
        raises can put the value it was inspecting into the message, and the log
        stream is an export path that none of the record-level checks cover.
        """
        logger.warning(
            "audit capture error code=%s audit_id=%s exception_type=%s",
            code,
            audit_id,
            type(exc).__name__,
        )

    def _metadata(
        self, record: NodeAuditRecord, decision: CaptureDecision, summaries: dict[str, Any]
    ) -> dict[str, Any]:
        """Project the producer's metadata and stamp the capture decision onto it."""
        if decision is CaptureDecision.CONTENT:
            metadata = self._scrub(dict(record.execution_metadata))
            if type(metadata) is not dict:
                metadata = {}
        else:
            metadata, summaries["execution_metadata"] = self._projection.metadata(
                record.execution_metadata
            )
        metadata[CAPTURE_METADATA_KEY] = {
            "classification": decision.value,
            "content_retained": decision is CaptureDecision.CONTENT,
            "dropped_fields": summaries,
        }
        return metadata

    def _dropped_content(self, record: NodeAuditRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        """Empty every content channel, returning the field updates and their summaries."""
        summarize = self._projection.summarize
        update: dict[str, Any] = {}
        summaries: dict[str, Any] = {}
        for name in _EMPTIED_MAPPING_FIELDS:
            summaries[name] = summarize(getattr(record, name))
            update[name] = {}
        for name in _EMPTIED_LIST_FIELDS:
            summaries[name] = summarize(getattr(record, name))
            update[name] = []
        for name in _EMPTIED_TEXT_FIELDS:
            summaries[name] = summarize(getattr(record, name))
            update[name] = None
        summaries["tool_calls"] = summarize(
            [
                {"arguments": call.arguments, "outcome": call.outcome, "error": call.error}
                for call in record.tool_calls
            ]
        )
        update["tool_calls"] = [self._strip_tool_call(call) for call in record.tool_calls]
        summaries["memory_interactions"] = summarize(
            [item.value for item in record.memory_interactions]
        )
        update["memory_interactions"] = [
            self._strip_memory(item) for item in record.memory_interactions
        ]
        summaries["approval_actions"] = summarize(
            [dict(action.metadata) for action in record.approval_actions]
        )
        update["approval_actions"] = [
            action.model_copy(update={"metadata": self._projection.metadata(action.metadata)[0]})
            for action in record.approval_actions
        ]
        return update, summaries

    def _scrubbed_content(self, record: NodeAuditRecord) -> dict[str, Any]:
        """Retain every content channel, scrubbed -- the explicitly classified branch."""
        names = (*_EMPTIED_MAPPING_FIELDS, *_EMPTIED_LIST_FIELDS, *_EMPTIED_TEXT_FIELDS)
        update: dict[str, Any] = {name: self._scrub(getattr(record, name)) for name in names}
        update["tool_calls"] = [
            call.model_copy(
                update={
                    "arguments": self._scrub(call.arguments),
                    "outcome": self._scrub(call.outcome),
                    "error": self._scrub(call.error),
                }
            )
            for call in record.tool_calls
        ]
        update["memory_interactions"] = [
            item.model_copy(update={"value": self._scrub(item.value)})
            for item in record.memory_interactions
        ]
        update["approval_actions"] = [
            action.model_copy(update={"metadata": self._scrub(action.metadata)})
            for action in record.approval_actions
        ]
        return update

    def _strip_tool_call(self, call: ToolCallRecord) -> ToolCallRecord:
        """Keep a tool call's identity, drop its arguments, result and failure text."""
        return call.model_copy(
            update={
                "arguments": {},
                "outcome": None,
                "error": None if call.error is None else REDACTED,
            }
        )

    def _strip_memory(self, item: MemoryAccessRecord) -> MemoryAccessRecord:
        """Keep a memory interaction's addressing, drop the value it moved."""
        return item.model_copy(update={"value": None, "key": self._scrub(item.key)})

    def _scrub(self, value: Any) -> Any:
        """Apply key redaction, then registered-secret masking, then PII filtering."""
        return self._filter_pii(self._secrets.redact(self._sanitizer.sanitize(value)))

    def _filter_pii(self, value: Any, *, depth: int = 0) -> Any:
        """Walk string leaves through the PII filter, normalizing them to plain ``str``.

        ``isinstance``, not the house exact-type check: here the widened branch
        filters *more*, and ``re.sub`` returns a plain ``str``, so no ``str``
        subclass survives into the persisted record.
        """
        if depth >= MAX_DEPTH:
            return REDACTED
        if isinstance(value, str):
            filtered, _ = self._pii.apply(value)
            return filtered
        if isinstance(value, Mapping):
            return {key: self._filter_pii(item, depth=depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [self._filter_pii(item, depth=depth + 1) for item in value]
        return value


__all__ = [
    "CAPTURE_METADATA_KEY",
    "DEFAULT_REDACT_KEYS",
    "AuditCapturePolicy",
    "CaptureClassifier",
    "CaptureDecision",
    "MetadataOnlyCaptureClassifier",
    "blank_record",
]
