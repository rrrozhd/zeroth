"""Data contracts for the signed run-start attestation.

The LangGraph gateway grants the ``enforced`` classification from evidence it
looks up by ``correlation_id``. That correlation is **client-suppliable**: it is
base64url-decoded out of the run config with no signature check (see
``zeroth.integrations.langgraph._correlation``). Binding the correlation -- and
every other identity field -- *inside* the signed payload is what stops one
run's attestation from being replayed under another correlation, tenant or
deployment.

Nothing here decides a governance level. The payload only carries what the
client asserts plus the identity it is bound to; the verifying side recomputes
the ceiling from state the server holds and treats ``claimed_level`` as an
upper-bound request that may only lower that determination.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_SCHEMA_VERSION = 1


class RunAttestationPayload(BaseModel):
    """Identity and tool-inventory claims a client attests at run start.

    Frozen and ``extra="forbid"``: the signed material must be exactly these
    fields, so an unrecognised key can never ride along unsigned and a field
    cannot be mutated after the digest is taken.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = _SCHEMA_VERSION

    correlation_id: str
    """Run correlation this attestation is bound to.

    Attacker-suppliable on the wire, which is precisely why it is signed here:
    evidence lookup keys on it, so an unbound correlation would let a valid
    attestation be replayed against a different run.
    """

    tenant_id: str
    deployment_ref: str

    graph_version: str
    adapter_version: str

    inventory_fingerprint: str
    """Digest of the governed tool inventory this run was started with."""

    inventory_coverage: str
    """Either ``"partial"`` or ``"complete"``.

    Kept a plain ``str`` rather than a ``Literal`` so parsing an attestation
    from untrusted JSON never raises; an unexpected value is a verification
    concern for the consuming provider, not a construction error.
    """

    tool_count: int = Field(ge=0)

    claimed_level: str
    """Governance level the client asserts for this run.

    **Advisory only.** The server recomputes the ceiling from state it holds;
    this value may only LOWER that determination, never raise it. A signature
    over this field proves who said it, not that it is true.
    """

    issued_at: datetime
    expires_at: datetime


_VOLATILE_CLAIM_FIELDS = frozenset({"issued_at", "expires_at"})
"""The only payload fields :func:`stable_claims` drops: the issuance window."""


def stable_claims(payload: RunAttestationPayload) -> dict[str, Any]:
    """Return every claim in ``payload`` except its server-assigned window.

    Neither the digest nor the signature can answer "are these the same
    claims". ``issued_at`` and ``expires_at`` are stamped from the server's
    clock as each request arrives, so an adapter that retries byte-identical
    claims after a lost response *normally* produces a different payload, a
    different digest and a different signature. This projection is what "the
    same claims" means for that comparison.

    "Normally", not always: two requests landing in the same microsecond read
    the same clock and build the same payload, so their envelopes match too.
    Such a duplicate is recognised by this same projection, on the same path as
    every other submission that did not insert: the route branches on whether
    *its own* write created the row, and every non-insert is then read back
    deployment-scoped and classified here and by the re-signature beside it.
    There is no cheaper envelope-equality shortcut ahead of this comparison --
    an earlier revision had one, and it was removed precisely because equal
    envelope *columns* are no evidence about the payload those columns are
    stored next to.

    It covers what the client submitted *and* what the server derived --
    ``inventory_coverage`` and ``tool_count`` come from the deployment's stored
    registration, so a retry arriving after a re-registration that changed
    either one is a genuinely different attestation and does not read as a
    repeat.

    **Not every re-registration changes a projected claim, and this projection
    does not pretend otherwise.** Swapping one tool for another leaves the
    coverage and the count untouched, and the ``inventory_fingerprint`` here is
    the *client's own submitted value*, identical in both requests by
    definition. Such a retry therefore still matches. That is the accepted
    consequence of leaving the fingerprint client-submitted: it binds a run to
    a tool set the client names, and the provider's check is that it equals the
    digest the server recomputed -- a check that happens when the evidence is
    read, not here.

    Exclusion rather than an allow-list, so a field added to
    :class:`RunAttestationPayload` later is compared by construction instead of
    being silently ignored. Both directions of drift fail closed: an added
    field only tightens the comparison, and renaming ``issued_at`` or
    ``expires_at`` -- which pydantic then ignores in ``exclude`` -- puts a
    timestamp back into the projection, so every retry stops matching rather
    than starting to match too eagerly.

    Args:
        payload: The attestation claims to project.

    Returns:
        A JSON-primitive mapping of every claim except ``issued_at`` and
        ``expires_at``, suitable for equality comparison.
    """
    return payload.model_dump(mode="json", exclude=set(_VOLATILE_CLAIM_FIELDS))


class SignedRunAttestation(BaseModel):
    """A run attestation payload plus its digest and keyed signature.

    Field names mirror the deployment-attestation surface in
    ``zeroth.service.api.audit_api`` (``digest`` / ``signature`` /
    ``signing_key_id`` / ``signing_algorithm``). The signature triple is
    nullable: an unsigned attestation (``NullSigner`` or no signer at all) is
    persisted as unsigned-legacy rather than as a signed-but-invalid record,
    and never verifies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: RunAttestationPayload
    digest: str
    signature: str | None = None
    signing_key_id: str | None = None
    signing_algorithm: str | None = None
