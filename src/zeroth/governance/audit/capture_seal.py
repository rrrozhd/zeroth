"""Proof that a record already crossed the capture boundary, and the boundary itself.

The capture policy runs on the delivery worker, but the delivery worker is not
the only way into the durable write: the orchestration runtime holds an
:class:`~zeroth.governance.audit.repository.AuditRepository` and calls
``write`` directly, and so does anything else that ever gets one. A boundary
every producer must remember to use is not a boundary, so ``write`` itself
applies the policy -- and then needs to know whether the record in its hands has
already been through it.

**The invariant: capture happens exactly once per record, and a producer cannot
make the repository believe it already happened.** The policy files its decision
under ``execution_metadata[CAPTURE_METADATA_KEY]``, which is the natural marker,
but that dict is producer-supplied: a caller can write the same shape by hand and
so bypass the very transform the marker attests to. The marker therefore carries
a process-local nonce minted at import, checked with a constant-time compare, and
:func:`strip_seal` removes it *before* the durable write -- so it never reaches
storage, never enters the digest, and cannot be read out of an audit row and
replayed.

Applying the policy twice is not harmless, which is why the check exists at all:
the second pass would summarize the already-emptied content channels and replace
the first pass's digests -- the hashes standing in for the dropped payload -- with
hashes of ``{}``.
"""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from zeroth.governance.audit.models import NodeAuditRecord

CAPTURE_METADATA_KEY = "audit_capture"
CAPTURE_SEAL_KEY = "seal"

# Minted once per process and never persisted or logged. A producer cannot
# guess it, and a reader of the audit store never sees it.
_PROCESS_SEAL = secrets.token_hex(16)


class RecordCapture(Protocol):
    """The transform :func:`capture_for_write` is allowed to apply.

    Satisfied by :class:`~zeroth.governance.audit.capture_policy.AuditCapturePolicy`.
    Declared structurally so this module stays below the policy in the import
    graph: the policy seals what it produces, and this module verifies it.
    """

    def apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Return the only version of ``record`` a durable write may persist."""
        ...


def seal_metadata(marker: dict[str, Any]) -> dict[str, Any]:
    """Stamp one capture marker with this process's nonce.

    Args:
        marker: The capture decision the policy is about to file under
            :data:`CAPTURE_METADATA_KEY`.

    Returns:
        The same mapping, carrying the nonce. Mutated in place because the
        caller has just built it and nothing else holds a reference.
    """
    marker[CAPTURE_SEAL_KEY] = _PROCESS_SEAL
    return marker


def is_sealed(record: NodeAuditRecord) -> bool:
    """Report whether ``record`` was produced by this process's capture policy."""
    marker = record.execution_metadata.get(CAPTURE_METADATA_KEY)
    if type(marker) is not dict:
        return False
    seal = marker.get(CAPTURE_SEAL_KEY)
    if type(seal) is not str:
        return False
    return hmac.compare_digest(seal, _PROCESS_SEAL)


def strip_seal(record: NodeAuditRecord) -> NodeAuditRecord:
    """Return ``record`` without the process nonce, ready for the durable write."""
    metadata = dict(record.execution_metadata)
    marker = metadata.get(CAPTURE_METADATA_KEY)
    if type(marker) is not dict or CAPTURE_SEAL_KEY not in marker:
        return record
    unsealed = {key: value for key, value in marker.items() if key != CAPTURE_SEAL_KEY}
    metadata[CAPTURE_METADATA_KEY] = unsealed
    return record.model_copy(update={"execution_metadata": metadata})


def capture_for_write(record: NodeAuditRecord, capture: RecordCapture) -> NodeAuditRecord:
    """Return the only version of ``record`` that may be persisted.

    Args:
        record: The record a producer submitted, by any path.
        capture: The transform to apply when the record has not been through
            one already.

    Returns:
        A captured, unsealed record. Idempotent: a record this process's policy
        produced is passed through untransformed, so the evidence its first pass
        recorded is not overwritten by a second.
    """
    captured = record if is_sealed(record) else capture.apply(record)
    return strip_seal(captured)


__all__ = [
    "CAPTURE_METADATA_KEY",
    "CAPTURE_SEAL_KEY",
    "RecordCapture",
    "capture_for_write",
    "is_sealed",
    "seal_metadata",
    "strip_seal",
]
