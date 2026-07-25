"""Capture classification and redaction applied before the durable audit write.

An audit record is evidence, and the producer that mints one is the wrong place
to decide what may be kept: the producer is holding the prompt, the tool
arguments and the model's answer, and a content channel that is closed only
because every caller remembered to clear it is open the first time one caller
forgets. This module owns that decision instead, and
:class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue` applies it to
every event it dequeues, so the transform sits on the single path into the
append-only write rather than at each of its call sites.

**The invariant: an event retains content only when it is classified into
content, and anything this stage cannot classify retains none.**
:class:`MetadataOnlyCaptureClassifier` is the default and answers
``metadata_only`` for every event, so a deployment that configures nothing gets
the closed posture. A classifier that raises, or returns a blank string, a
non-``str``, or a string this module does not recognise, is read the same way:
the unknown branch and the conservative branch are one branch, which is what
stops taxonomy drift from quietly opening the channel.

**Fail-closed twice over.** ``AuditCapturePolicy()`` with no arguments is already
a redacting policy -- deliberately the opposite posture from
``RuntimeAuditRecorder.redact``, which becomes a pass-through when no secret
resolver is injected. Value-based redaction can only mask secrets someone
registered, so "nobody registered any" must not resolve to "emit raw"; the
key-based and channel-based rules below need no registration and are always on.
And :meth:`AuditCapturePolicy.apply` never raises: it walks producer-supplied
payloads of arbitrary shape, so it can meet a value that will not serialize, a
mapping that cycles or a ``__len__`` that throws, and when it does the output is
:meth:`AuditCapturePolicy.blank` -- a record stripped of every content channel --
never the record that came in.

**What is removed and what is kept are equally load-bearing.** Under
``metadata_only`` the content channels (``input_snapshot``, ``output_snapshot``,
``validation_results``, ``condition_results``, ``stdout``, ``stderr``, each tool
call's ``arguments`` and ``outcome``, each memory interaction's ``value``) are
emptied and replaced by a SHA-256 digest, a key-and-type schema and an entry
count filed under ``execution_metadata[CAPTURE_METADATA_KEY]``. Everything that
makes the record useful without reproducing content survives untouched: identity
and lineage, ``status``, timing, ``token_usage``, ``cost_usd``, the actor,
approval actions and the digest-chain fields. A record whose evidentiary value
died with its content would be a slower way of not auditing.

**The primitives are reused, not reinvented.** Key-based redaction comes from
:class:`~zeroth.governance.audit.sanitizer.PayloadSanitizer`, masking of
registered secret values from
:class:`~zeroth.platform.secrets.redaction.SecretRedactor`, pattern detection
from :class:`~zeroth.governance.guardrails.content.PIIFilter`. All three are
complements and none carries the guarantee -- a key rule cannot see a secret
under an unexpected key, a value rule cannot see a secret nobody registered, a
pattern cannot see what it does not match -- so the channel drop above is what
holds. The ``PIIFilter`` is built with ``email``, ``ssn`` and ``credit_card``
only: its own docstring warns the phone heuristic is false-positive-prone, and
it is (any ten-digit run matches, timestamps and numeric ids included), so it is
left out of a filter that runs over *surviving* metadata. The other three are
precise, the card pattern is Luhn-checked, and a false positive there costs one
masked metadata value rather than a lost audit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

from zeroth.governance.audit.models import (
    ApprovalActionRecord,
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

_REDACTED = "***REDACTED***"
# Bounds the two recursive walks so a deep or cyclic payload cannot exhaust the
# stack inside the delivery worker.
_MAX_DEPTH = 6
# A key name is schema; a string long enough to be a credential is not.
_MAX_SCHEMA_KEY_CHARS = 64

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
    front of every audit write.
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


def _digest(value: Any) -> str | None:
    """Hash a payload's canonical rendering, or return ``None`` if it will not render."""
    try:
        rendered = json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _count(value: Any) -> int:
    """Count a dropped payload's entries (or characters), never inspecting them."""
    try:
        return 0 if value is None else len(value)
    except TypeError:
        return 1


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
            logger.warning("audit capture failed for audit_id %s: %s", record.audit_id, exc)
            return self.blank(record)

    def blank(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Return ``record`` with every content channel emptied and nothing summarized.

        The last-resort shape, reached when the policy itself fails on a payload:
        the only output safe without having successfully inspected the input is
        one that carries none of it.
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

    def _apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Classify, transform the content channels, and stamp the decision."""
        decision = self._decide(record)
        summaries: dict[str, Any] = {}
        if decision is CaptureDecision.CONTENT:
            update = self._scrubbed_content(record)
        else:
            update, summaries = self._dropped_content(record)
        update["error"] = self._scrub(record.error)
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
            logger.warning("capture classifier failed for audit_id %s: %s", record.audit_id, exc)
            return CaptureDecision.METADATA_ONLY
        if type(raw) is not str or not raw:
            return CaptureDecision.METADATA_ONLY
        if raw == CaptureDecision.CONTENT.value:
            return CaptureDecision.CONTENT
        return CaptureDecision.METADATA_ONLY

    def _metadata(
        self, record: NodeAuditRecord, decision: CaptureDecision, summaries: dict[str, Any]
    ) -> dict[str, Any]:
        """Scrub the producer's metadata and stamp the capture decision onto it."""
        metadata = self._scrub(dict(record.execution_metadata))
        if type(metadata) is not dict:
            metadata = {}
        metadata[CAPTURE_METADATA_KEY] = {
            "classification": decision.value,
            "content_retained": decision is CaptureDecision.CONTENT,
            "dropped_fields": summaries,
        }
        return metadata

    def _dropped_content(self, record: NodeAuditRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        """Empty every content channel, returning the field updates and their summaries."""
        update: dict[str, Any] = {}
        summaries: dict[str, Any] = {}
        for name in _EMPTIED_MAPPING_FIELDS:
            summaries[name] = self._summarize(getattr(record, name))
            update[name] = {}
        for name in _EMPTIED_LIST_FIELDS:
            summaries[name] = self._summarize(getattr(record, name))
            update[name] = []
        for name in _EMPTIED_TEXT_FIELDS:
            summaries[name] = self._summarize(getattr(record, name))
            update[name] = None
        summaries["tool_calls"] = self._summarize(
            [{"arguments": call.arguments, "outcome": call.outcome} for call in record.tool_calls]
        )
        update["tool_calls"] = [self._strip_tool_call(call) for call in record.tool_calls]
        summaries["memory_interactions"] = self._summarize(
            [item.value for item in record.memory_interactions]
        )
        update["memory_interactions"] = [
            self._strip_memory(item) for item in record.memory_interactions
        ]
        update["approval_actions"] = self._scrub_approvals(record.approval_actions)
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
        update["approval_actions"] = self._scrub_approvals(record.approval_actions)
        return update

    def _strip_tool_call(self, call: ToolCallRecord) -> ToolCallRecord:
        """Keep a tool call's identity and error, drop its arguments and result."""
        return call.model_copy(
            update={"arguments": {}, "outcome": None, "error": self._scrub(call.error)}
        )

    def _strip_memory(self, item: MemoryAccessRecord) -> MemoryAccessRecord:
        """Keep a memory interaction's addressing, drop the value it moved."""
        return item.model_copy(update={"value": None, "key": self._scrub(item.key)})

    def _scrub_approvals(self, actions: list[ApprovalActionRecord]) -> list[ApprovalActionRecord]:
        """Scrub approval metadata in place of dropping it: it is decision evidence."""
        scrub = self._scrub
        return [item.model_copy(update={"metadata": scrub(item.metadata)}) for item in actions]

    def _summarize(self, value: Any) -> dict[str, Any]:
        """Describe a dropped payload -- digest, shape and size -- without reproducing it."""
        return {"sha256": _digest(value), "schema": self._schema(value), "count": _count(value)}

    def _schema(self, value: Any, *, depth: int = 0) -> Any:
        """Describe a payload's shape: key names and value type names, never values."""
        if depth >= _MAX_DEPTH:
            return "..."
        if isinstance(value, Mapping):
            return {
                self._schema_key(key): self._schema(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            # The first element stands for the list's shape; its length is in ``count``.
            return [self._schema(value[0], depth=depth + 1)] if value else []
        return type(value).__name__

    def _schema_key(self, key: Any) -> str:
        """Retain a key name as schema unless it is long enough to be a credential."""
        name = str(key)
        if len(name) > _MAX_SCHEMA_KEY_CHARS:
            return _REDACTED
        scrubbed = self._scrub(name)
        return scrubbed if type(scrubbed) is str else _REDACTED

    def _scrub(self, value: Any) -> Any:
        """Apply key redaction, then registered-secret masking, then PII filtering."""
        return self._filter_pii(self._secrets.redact(self._sanitizer.sanitize(value)))

    def _filter_pii(self, value: Any, *, depth: int = 0) -> Any:
        """Walk string leaves through the PII filter, normalizing them to plain ``str``.

        ``isinstance``, not the house exact-type check: here the widened branch
        filters *more*, and ``re.sub`` returns a plain ``str``, so no ``str``
        subclass survives into the persisted record.
        """
        if depth >= _MAX_DEPTH:
            return _REDACTED
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
]
