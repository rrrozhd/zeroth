"""Tenant-scoped persistence for registered inventories and run attestations.

Two append-only tables from migration 016 back the enforcement classification,
and each is written with one rule that the verifying provider depends on.

**The signed bytes are stored verbatim.** ``attestation_digest`` hashes the
canonical JSON of the *whole* payload, so a reload that reassembles the payload
out of the flat columns would risk a round-trip difference -- a dropped
timezone, a re-ordered key -- and the recomputed digest would then disagree
with the stored one for reasons that have nothing to do with tampering.
:meth:`RunAttestationRepository.find_by_correlation` therefore rebuilds the
payload from ``payload_json`` alone and treats every flat column as query and
index material.

**First write wins per run.** ``run_attestations`` carries UNIQUE
``(tenant_id, correlation_id)`` and :meth:`RunAttestationRepository.record`
inserts with ``ON CONFLICT DO NOTHING``. A run's evidence is fixed by the first
attestation the server accepts, so a second attestation arriving under the same
correlation cannot replace weaker evidence with stronger. Registrations, by
contrast, are genuinely append-only history: the newest row for a deployment is
its current inventory.

**Tenancy is part of every key, never a filter applied afterwards.** Every
statement here names ``tenant_id``, so a lookup cannot cross the boundary even
when the correlation id it is given was chosen by an attacker.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.governance.attestations.inventory import (
    RegisteredTool,
    recompute_inventory_fingerprint,
    refuse_repeated_tool_names,
)
from zeroth.governance.attestations.payload import (
    RunAttestationPayload,
    SignedRunAttestation,
)
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncDatabase

_REGISTRATION_COLUMNS = (
    "registration_id, tenant_id, deployment_ref, graph_version, adapter_version, "
    "inventory_fingerprint, coverage, tool_count, tools_json, registered_at, "
    "tool_identities_json"
)

_SELECT_LATEST_REGISTRATION = (
    f"SELECT {_REGISTRATION_COLUMNS} FROM tool_inventory_registrations "
    "WHERE tenant_id = ? AND deployment_ref = ? "
    "ORDER BY registered_at DESC LIMIT 1"
)

_ATTESTATION_COLUMNS = (
    "attestation_id, tenant_id, correlation_id, deployment_ref, graph_version, "
    "adapter_version, inventory_fingerprint, inventory_coverage, tool_count, "
    "claimed_level, payload_json, digest, signature, signing_key_id, "
    "signing_algorithm, issued_at, expires_at, created_at"
)

_SELECT_ATTESTATION = (
    "SELECT attestation_id, payload_json, digest, signature, signing_key_id, "
    "signing_algorithm FROM run_attestations "
    "WHERE tenant_id = ? AND correlation_id = ?"
)
"""The unscoped read of the attestation in force for one run.

``attestation_id`` is selected because :meth:`RunAttestationRepository.record`
compares it against the id it tried to insert; that comparison is the only way
to tell "my insert won" from "a byte-identical row was already here", which two
concurrent identical requests produce whenever they land in the same
microsecond. Adding a *column* here is safe for every consumer --
:func:`_row_to_attestation` reads by key -- unlike adding a predicate, see below.
"""

_SELECT_ATTESTATION_FOR_DEPLOYMENT = f"{_SELECT_ATTESTATION} AND deployment_ref = ?"
"""The deployment-scoped read, kept separate from :data:`_SELECT_ATTESTATION`.

:meth:`RunAttestationRepository.record` reuses the unscoped statement to read
back the winning row, and it binds only tenant and correlation -- the pair the
UNIQUE constraint is on. Widening the shared constant with a third predicate
would silently break that read rather than the caller that wanted the scope.
"""


class InventoryRegistration(BaseModel):
    """One registration event: what an SDK declared for a deployment.

    ``coverage`` stays a plain ``str`` for the same reason
    ``RunAttestationPayload.inventory_coverage`` does -- an unexpected token is
    a verification concern for the reading provider, not a construction error,
    and must not make a stored row unloadable.

    **``inventory_fingerprint`` and ``tool_count`` are derived, not accepted.**
    Whatever a caller passes for either is discarded and replaced by the value
    :data:`~zeroth.governance.attestations.inventory.recompute_inventory_fingerprint`
    computes from ``tools``. This is enforced in the model rather than in the
    route so that a lying registration is not merely rejected at one entry
    point but *unconstructable*: the audited attack registered
    ``tool_count=999`` against an empty tool set, and no code path -- route,
    test double, or future caller -- can express that here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str
    deployment_ref: str
    graph_version: str
    adapter_version: str
    coverage: str
    tools: tuple[RegisteredTool, ...] = ()
    """The governed tool identities this registration declared.

    Name *and* fingerprint. Names alone would let a tool be swapped for another
    under the same label without changing the registration -- and the aggregate
    digest below is computed over these pairs, so the identities are what the
    enforcement decision actually rests on.
    """

    inventory_fingerprint: str = ""
    tool_count: int = Field(default=0, ge=0)

    registration_id: str = Field(default_factory=lambda: uuid4().hex)
    registered_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _derive_inventory_facts(self) -> InventoryRegistration:
        """Refuse a repeated tool name, then derive the digest and count.

        The refusal runs *before* the digest is recomputed, because a repeated
        name is not a digest the caller got wrong -- it recomputes perfectly
        well over both pairs, and that is the defect. Registering
        ``("send_email", "fp-impostor")`` alongside the genuine
        ``("send_email", "fp-legit")`` authorizes the impostor under a governed
        label while leaving coverage ``complete``, which defeats the exact
        identity match admission rests on. Refusing here makes such a
        registration unconstructable rather than merely rejected at one route.
        """
        refuse_repeated_tool_names(self.tools)
        fingerprint = recompute_inventory_fingerprint(self.tools)
        count = len(self.tools)
        if self.inventory_fingerprint == fingerprint and self.tool_count == count:
            return self
        # ``frozen=True`` blocks attribute assignment, so the derived values
        # are written through the underlying dict -- the same mechanism
        # pydantic itself uses for a frozen model's fields.
        object.__setattr__(self, "inventory_fingerprint", fingerprint)
        object.__setattr__(self, "tool_count", count)
        return self


def _row_to_registration(row: dict[str, Any]) -> InventoryRegistration:
    """Rebuild a registration from one ``tool_inventory_registrations` row.

    Identities come from ``tool_identities_json``. A row predating migration
    017 has ``NULL`` there and is rebuilt with no identities at all -- which is
    the fail-closed reading, because such a row's stored digest and count were
    client-certified under the scheme audit round 1 rejected. Migration 017
    additionally downgrades those rows' ``coverage``, so they cannot complete a
    manifest even before this hydration is reached.

    **A row whose identities repeat a name is deliberately unloadable.**
    :meth:`InventoryRegistration._derive_inventory_facts` now refuses a
    repeated name, so hydrating such a row raises ``ValidationError`` out of
    this function rather than yielding a registration that authorizes two
    callables under one label. That is the intended reading: unreadable is no
    evidence. Every caller of
    :meth:`InventoryRegistrationRepository.latest_for_deployment` already
    treats a raise as absence of evidence rather than as a fault --
    ``ToolDecisionService._is_registered`` answers ``capability_denied``,
    ``PersistedCapabilityEvidenceProvider`` answers with no evidence, and the
    attestation route answers 503 -- so the failure mode is refusal on every
    path, never an admitted impostor. Such a row can only predate this rule,
    since it can no longer be written.
    """
    raw_identities = row.get("tool_identities_json")
    identities = () if raw_identities is None else json.loads(str(raw_identities))
    return InventoryRegistration(
        registration_id=str(row["registration_id"]),
        tenant_id=str(row["tenant_id"]),
        deployment_ref=str(row["deployment_ref"]),
        graph_version=str(row["graph_version"]),
        adapter_version=str(row["adapter_version"]),
        coverage=str(row["coverage"]),
        tools=tuple(RegisteredTool.model_validate(entry) for entry in identities),
        registered_at=datetime.fromisoformat(str(row["registered_at"])),
    )


class InventoryRegistrationRepository:
    """Append and read tool-inventory registrations, scoped per tenant."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def register(self, registration: InventoryRegistration) -> InventoryRegistration:
        """Append one registration event for a deployment.

        Args:
            registration: The declared inventory to record. Its
                ``registration_id`` and ``registered_at`` default to a fresh
                id and the current time when the caller does not set them.

        Returns:
            The registration exactly as stored, so a caller that relied on the
            defaults learns the identity it was given.
        """
        async with self._database.transaction(write_lock=True) as connection:
            await connection.execute(
                f"""
                INSERT INTO tool_inventory_registrations ({_REGISTRATION_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    registration.registration_id,
                    registration.tenant_id,
                    registration.deployment_ref,
                    registration.graph_version,
                    registration.adapter_version,
                    registration.inventory_fingerprint,
                    registration.coverage,
                    registration.tool_count,
                    # ``tools_json`` keeps its 016 shape -- the bare names --
                    # because the column is NOT NULL and the names remain
                    # useful for inspection. It is never read back: the
                    # identities the digest is computed over live in
                    # ``tool_identities_json``.
                    json.dumps([tool.name for tool in registration.tools]),
                    registration.registered_at.isoformat(),
                    json.dumps(
                        [
                            {"name": tool.name, "fingerprint": tool.fingerprint}
                            for tool in registration.tools
                        ]
                    ),
                ),
            )
        return registration

    async def latest_for_deployment(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> InventoryRegistration | None:
        """Load the most recently registered inventory for one deployment.

        Args:
            tenant_id: The tenant the deployment belongs to.
            deployment_ref: The deployment whose inventory is wanted.

        Returns:
            The newest registration, or ``None`` when this tenant has never
            registered an inventory for that deployment.
        """
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                _SELECT_LATEST_REGISTRATION,
                (tenant_id, deployment_ref),
            )
        return None if row is None else _row_to_registration(row)


def _envelope_of(row: dict[str, Any]) -> tuple[str, str | None, str | None, str | None]:
    """Return one row's signed envelope: digest, signature, key id, algorithm.

    ``NULL`` reads back as ``None`` rather than as the string ``"None"``, so an
    unsigned stored attestation compares equal to an unsigned submitted one and
    unequal to a signed one -- the case that made a digest-only comparison
    report an unverifiable attestation as the caller's own.
    """
    return (
        str(row["digest"]),
        None if row["signature"] is None else str(row["signature"]),
        None if row["signing_key_id"] is None else str(row["signing_key_id"]),
        None if row["signing_algorithm"] is None else str(row["signing_algorithm"]),
    )


class AttestationRecordOutcome(BaseModel):
    """What happened when one attestation was offered to the store.

    Two facts, because one bool cannot carry both and the caller needs each for
    a different purpose:

    * :attr:`authoritative` answers "is the evidence in force mine" — what the
      submitting client must be told.
    * :attr:`inserted` answers "did my write create that row" — what an
      operator counting stored attestations needs.

    They agree except in one case, and that case is why this type exists: two
    concurrent identical requests stamped with the same ``issued_at`` produce
    byte-identical envelopes, so the loser is authoritative without having
    inserted anything. Reported as one bool, that loser was indistinguishable
    from a fresh write and inflated the ``recorded`` metric.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    inserted: bool
    """True iff *this* call's row is the one now in force."""

    authoritative: bool
    """True iff the envelope in force is byte-identical to the submitted one.

    Implied by :attr:`inserted`, and reachable without it.

    **Not sufficient on its own to treat a stored row as the caller's own.**
    This compares the envelope *columns*; it does not recompute the digest over
    ``payload_json``, and it does not check the flat ``deployment_ref`` against
    the deployment named inside the signed payload. For a row this repository
    wrote the two agree by construction, but an internally inconsistent row --
    one edited outside this class, or written by an older revision -- can carry
    an envelope equal to a submission whose payload differs. A caller deciding
    what to disclose must therefore re-derive from the stored payload rather
    than trust this flag; the attestation route does exactly that, via
    ``is_identical_resubmission`` against a deployment-scoped read.
    """


def _row_to_attestation(row: dict[str, Any]) -> SignedRunAttestation:
    """Rebuild a signed attestation from one ``run_attestations`` row.

    The payload comes back out of ``payload_json`` rather than the flat
    columns, so the bytes that were signed are the bytes that get verified.
    """
    payload = RunAttestationPayload.model_validate_json(str(row["payload_json"]))
    return SignedRunAttestation(
        payload=payload,
        digest=str(row["digest"]),
        signature=None if row["signature"] is None else str(row["signature"]),
        signing_key_id=None if row["signing_key_id"] is None else str(row["signing_key_id"]),
        signing_algorithm=(
            None if row["signing_algorithm"] is None else str(row["signing_algorithm"])
        ),
    )


class RunAttestationRepository:
    """Append and read signed run-start attestations, scoped per tenant."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def record(self, signed: SignedRunAttestation) -> AttestationRecordOutcome:
        """Store an attestation unless the run already has one.

        ``ON CONFLICT DO NOTHING`` on ``(tenant_id, correlation_id)`` makes a
        second attestation for the same run a no-op rather than an integrity
        error, and keeps the statement portable across SQLite and PostgreSQL.
        The no-op is the intended outcome: the evidence a run is judged on is
        fixed at first write, so a later submission cannot upgrade it.

        **The outcome is returned rather than swallowed.** A caller that could
        not tell "recorded" from "discarded because an earlier attestation is
        in force" would have to report success either way, telling a client its
        attestation is the run's evidence when some other attestation is. The
        insert and the ``SELECT`` that reads back the winner share one
        write-locked transaction, so no competing writer can slip between them
        and make the answer describe a third row.

        Args:
            signed: The attestation to persist, with its digest and signature.

        Returns:
            An :class:`AttestationRecordOutcome`. ``authoritative`` is True when
            the attestation now in force is byte-identical to this one -- either
            because this insert won, or because an earlier submission of *the
            same signed envelope* did; False when the run's evidence is some
            other attestation and this submission was discarded. A False is not
            an error -- the run has evidence, just not this evidence.
            ``inserted`` narrows that to whether *this* write is the row in
            force.

            **The equality is the whole signed envelope, not the payload
            digest.** ``attestation_digest`` covers the claims and nothing
            else: it excludes the signature, the signing key id and the
            algorithm. Two submissions can therefore agree on every claim while
            only one of them verifies -- a key rotated and the old key retired,
            or an unsigned attestation already in force under the same
            correlation. Comparing digests alone answered True in both cases,
            telling the caller its attestation is what the run will be judged
            on while the evidence actually in force does not verify at all.
            Under envelope equality the caller is told False, which is the
            truth: some *other* attestation holds the run.

            **Why ``inserted`` is not merely ``authoritative`` restated.** The
            two coincide for every *sequential* caller, which is what made one
            bool look sufficient. They come apart under concurrency: two
            identical requests stamped with the same ``issued_at`` build the
            same payload, and deterministic signing then makes their envelopes
            equal, so the loser is authoritative on a row it did not write. The
            id comparison below is what separates them -- a rowcount cannot,
            because ``ON CONFLICT DO NOTHING`` reports zero affected rows for a
            successful insert on some drivers.

            **Neither field is the HTTP retry rule.** A client retrying the
            same request usually does not reach here with the same payload at
            all, because the route stamps a fresh ``issued_at`` on arrival, so
            an honest retry is normally a *different* payload and both fields
            are then False. Recognising that case needs the claims compared
            apart from the issuance window, which is
            ``is_identical_resubmission``'s job at the route, not this one's.
            The same-microsecond collision above is the exception that proves
            the rule rather than an alternative to it: there the envelopes match
            and ``authoritative`` is True, which is why the route must consult
            ``inserted`` before it counts a submission as newly recorded.
        """
        payload = signed.payload
        candidate_id = uuid4().hex
        async with self._database.transaction(write_lock=True) as connection:
            await connection.execute(
                f"""
                INSERT INTO run_attestations ({_ATTESTATION_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, correlation_id) DO NOTHING
                """,
                (
                    candidate_id,
                    payload.tenant_id,
                    payload.correlation_id,
                    payload.deployment_ref,
                    payload.graph_version,
                    payload.adapter_version,
                    payload.inventory_fingerprint,
                    payload.inventory_coverage,
                    payload.tool_count,
                    payload.claimed_level,
                    payload.model_dump_json(),
                    signed.digest,
                    signed.signature,
                    signed.signing_key_id,
                    signed.signing_algorithm,
                    payload.issued_at.isoformat(),
                    payload.expires_at.isoformat(),
                    utc_now().isoformat(),
                ),
            )
            winner = await connection.fetch_one(
                _SELECT_ATTESTATION,
                (payload.tenant_id, payload.correlation_id),
            )
        # Read back rather than trusting a rowcount: ``ON CONFLICT DO NOTHING``
        # reports zero affected rows on some drivers even for a successful
        # insert. ``_SELECT_ATTESTATION`` returns the id and the signature
        # triple alongside the digest, so both answers cost no extra read.
        if winner is None:
            return AttestationRecordOutcome(inserted=False, authoritative=False)
        return AttestationRecordOutcome(
            inserted=str(winner["attestation_id"]) == candidate_id,
            authoritative=_envelope_of(winner)
            == (
                signed.digest,
                signed.signature,
                signed.signing_key_id,
                signed.signing_algorithm,
            ),
        )

    async def find_by_correlation(
        self,
        tenant_id: str,
        correlation_id: str,
    ) -> SignedRunAttestation | None:
        """Load the attestation a tenant recorded for one run.

        Args:
            tenant_id: The tenant the run belongs to.
            correlation_id: The run correlation, as attested.

        Returns:
            The stored attestation, or ``None`` when this tenant has no
            attestation under that correlation. A correlation belonging to a
            different tenant reads as absent, never as another tenant's row.
        """
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(_SELECT_ATTESTATION, (tenant_id, correlation_id))
        return None if row is None else _row_to_attestation(row)

    async def find_for_deployment(
        self,
        tenant_id: str,
        deployment_ref: str,
        correlation_id: str,
    ) -> SignedRunAttestation | None:
        """Load the attestation a run recorded *under one deployment*.

        The lookup a trust boundary wants.
        :meth:`find_by_correlation` answers "what is in force for this run",
        which is the right question for reporting a conflict back to a
        submitter, and the wrong one for deciding what a deployment may be
        judged on: a correlation id is attacker-suppliable, so a caller holding
        deployment A can otherwise be handed an attestation signed for
        deployment B and pair it with A's registered inventory.

        **Scope is decided on the signed payload, not only on the flat
        columns.** The ``WHERE`` clause narrows by ``tenant_id``,
        ``correlation_id`` and ``deployment_ref`` as written at insert time,
        and those columns are index material: nothing has checked them against
        the payload that was actually signed. For a row this repository wrote
        they agree, but a row whose flat columns say one thing and whose signed
        payload says another would otherwise be handed to the wrong caller --
        and the caller's next move is to report its digest and expiry back over
        the wire, which is the disclosure the scoped read exists to prevent.
        Re-checking makes the guarantee in the sentence below true rather than
        merely intended; an inconsistent row reads as absent, which is no
        evidence.

        **All three identity fields are re-checked, not just the two that name
        the deployment.** Checking tenant and deployment while trusting the
        ``correlation_id`` column left the same defect one field over: a row
        whose column says run A while its payload says run B still read as
        present, and the route then reported run B's digest and expiry in run
        A's conflict. That is a narrower leak than the cross-deployment one --
        same tenant, same deployment, and it needs write access to the table --
        but it is the identical shape, and a lookup that re-derives two of
        three identities from the payload is the inconsistency that produced
        this defect in the first place. This mirrors
        ``PersistedCapabilityEvidenceProvider._binds_to_this_provider``, which
        has always checked all three; the two are now the same rule stated in
        two places rather than two different rules.

        Args:
            tenant_id: The tenant the run belongs to.
            deployment_ref: The deployment the attestation must name.
            correlation_id: The run correlation, as attested.

        Returns:
            The stored attestation, or ``None`` when this tenant has no
            attestation under that correlation *for that deployment*. Another
            deployment's attestation reads as absent, never as this one's, and
            so does an attestation whose signed payload names another run.
        """
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                _SELECT_ATTESTATION_FOR_DEPLOYMENT,
                (tenant_id, correlation_id, deployment_ref),
            )
        if row is None:
            return None
        attestation = _row_to_attestation(row)
        if (
            attestation.payload.tenant_id != tenant_id
            or attestation.payload.deployment_ref != deployment_ref
            or attestation.payload.correlation_id != correlation_id
        ):
            return None
        return attestation


__all__ = [
    "AttestationRecordOutcome",
    "InventoryRegistration",
    "InventoryRegistrationRepository",
    "RunAttestationRepository",
]
