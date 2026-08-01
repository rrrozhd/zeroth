"""HTTP request and response bodies for the tool-enforcement surface.

These models live in ``zeroth.governance`` rather than next to the routes on
purpose. ``tests/architecture/test_library_surface.py`` scans every
``zeroth/service/api/*_api.py`` (and every ``models.py`` / ``schemas.py``) for
pydantic models *defined* there and requires each to carry a protected legacy
identity. A subsystem introduced after that fixture was frozen has no legacy
identity to carry, so its wire models are defined here and imported by the
route module instead.

**No submission model carries a tenant or a principal.** Every body below is
client-supplied, and a client-supplied ``tenant_id`` is an invitation to write
one tenant's evidence into another's. The identity halves of
:class:`~zeroth.governance.decisions.request.DecisionRequest` and
:class:`~zeroth.governance.attestations.payload.RunAttestationPayload` are
filled in by the route from the authenticated principal and from the
deployment record the server holds, never from these bodies.

**``deployment_ref`` is the one identity a body does carry**, because a client
must be able to say which deployment it means. It is not trusted: the route
loads that deployment from the server's own store and refuses any deployment
the caller's scope does not cover, so the field selects a deployment rather
than asserting one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zeroth.governance.attestations.inventory import (
    RegisteredTool,
    refuse_repeated_tool_names,
)
from zeroth.governance.decisions.request import NormalizedAction


class DecisionSubmission(BaseModel):
    """One tool call submitted for a verdict.

    The client half of
    :class:`~zeroth.governance.decisions.request.DecisionRequest`: the action,
    the deployment it was made from, and the replay token. ``tenant_id`` and
    ``principal_id`` are deliberately absent -- the route supplies them from
    the authenticated principal.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    deployment_ref: str = Field(min_length=1)
    action: NormalizedAction
    idempotency_key: str = Field(min_length=1)
    policy_bindings: tuple[str, ...] = ()


class InventorySubmission(BaseModel):
    """A tool inventory an adapter declares for one deployment."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str = Field(min_length=1)
    graph_version: str
    adapter_version: str
    coverage: str
    """Either ``"partial"`` or ``"complete"``.

    A plain ``str`` for the same reason
    :attr:`~zeroth.governance.attestations.store.InventoryRegistration.coverage`
    is: an unexpected token is a verification concern for the reading provider,
    which treats anything other than ``"complete"`` as incomplete coverage, and
    must not turn into a 422 that hides the declaration entirely.

    This remains the client's declaration, because no server-side source of
    truth exists for "did you tell me about every tool your graph binds". What
    changed after audit round 1 is that it no longer travels with a
    self-chosen digest and count: a ``complete`` claim is now checked against
    identities the client had to enumerate.
    """

    tools: tuple[RegisteredTool, ...] = ()
    """The governed tool identities, each a name and a fingerprint.

    ``inventory_fingerprint`` and ``tool_count`` are absent from this body on
    purpose. Both are recomputed from these identities by
    :class:`~zeroth.governance.attestations.store.InventoryRegistration`, so a
    caller cannot declare a digest its own tool set does not produce -- which
    is precisely the registration half of the audited attack.
    """

    @field_validator("tools")
    @classmethod
    def _refuse_repeated_names(
        cls,
        tools: tuple[RegisteredTool, ...],
    ) -> tuple[RegisteredTool, ...]:
        """Refuse a body naming the same tool twice.

        The rule is carried *here as well as* on
        :class:`~zeroth.governance.attestations.store.InventoryRegistration`,
        and the duplication is the point. The store model is what makes the
        state unconstructable, but the route builds it inside the handler body
        (``zeroth/service/api/enforcement_api.py``), where a pydantic
        ``ValidationError`` is an unhandled exception and therefore a 500. A
        request body validated at the transport boundary answers **422**, so
        the caller learns its registration was malformed rather than that the
        server broke. This validator is what makes the store model's raise
        unreachable from the route.
        """
        refuse_repeated_tool_names(tools)
        return tools


class InventoryAck(BaseModel):
    """The identity the server assigned to a recorded registration."""

    model_config = ConfigDict(extra="forbid")

    registration_id: str
    deployment_ref: str
    registered_at: datetime


class AttestationSubmission(BaseModel):
    """Run-start claims a client asks the server to bind and record.

    The client never signs: the signing key lives on the server (see
    ``zeroth.platform.signing``), so the signature attributes the claim to the
    server having accepted it from an authenticated principal. ``issued_at``
    and ``expires_at`` are likewise server-assigned -- a client-chosen validity
    window would let a run mint an attestation that never expires.
    """

    model_config = ConfigDict(extra="forbid")

    correlation_id: str = Field(min_length=1)
    deployment_ref: str = Field(min_length=1)
    graph_version: str
    adapter_version: str
    inventory_fingerprint: str
    """The digest of the tool set this run actually started with.

    Deliberately still client-submitted, and the one inventory field that is.
    It is a *binding*, not an authority claim: the provider's whole check is
    that this value equals the digest the server recomputed for the
    deployment's registered inventory. Deriving it from that registration --
    as the audit's wording suggested -- would compare the value to itself,
    making the comparison vacuously true and ``fingerprint_drift`` (R12)
    unfalsifiable. ``inventory_coverage`` and ``tool_count`` were the genuine
    self-certification vectors and are gone from this body; the server takes
    them from the stored registration instead.
    """

    claimed_level: str
    """Advisory ceiling only; the server recomputes what it can prove."""


class AttestationAck(BaseModel):
    """Whether the submitted attestation is the one now in force.

    ``run_attestations`` is first-write-wins per ``(tenant, correlation)``, so a
    submission can be accepted by the transport and still not be the evidence
    the run is judged on. :attr:`authoritative` is what distinguishes the two;
    the digest and expiry describe the attestation actually in force, which is
    the submitted one only when :attr:`authoritative` is true.
    """

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    authoritative: bool
    status: str
    digest: str
    expires_at: datetime


class HeartbeatSubmission(BaseModel):
    """One liveness ping from a deployment's adapter."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str = Field(min_length=1)
    graph_version: str | None = None
    adapter_version: str | None = None
    reported_level: str


class HeartbeatAck(BaseModel):
    """The identity and observation time the server recorded for a ping."""

    model_config = ConfigDict(extra="forbid")

    heartbeat_id: str
    deployment_ref: str
    observed_at: datetime


class DeploymentEnforcementStatus(BaseModel):
    """A deployment's last-known governance level, recomputed server-side."""

    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    governance_level: str
    observed_at: datetime | None = None
    stale_after_seconds: float


class RunEnforcementStatus(BaseModel):
    """One run's governance level, recomputed from stored evidence."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    governance_level: str


__all__ = [
    "AttestationAck",
    "AttestationSubmission",
    "DecisionSubmission",
    "DeploymentEnforcementStatus",
    "HeartbeatAck",
    "HeartbeatSubmission",
    "InventoryAck",
    "InventorySubmission",
    "RunEnforcementStatus",
]
