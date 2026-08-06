"""ZER-8 tool-enforcement HTTP surface.

The transport for four things the governance layer already implements and had
no way to reach over a wire: a tool decision
(:mod:`zeroth.governance.decisions`), a declared tool inventory, a signed
run-start attestation, and a deployment heartbeat
(:mod:`zeroth.governance.attestations`). Nothing here decides anything -- every
route resolves identity, hands the domain service a request built from
server-held state, and maps the outcome onto a status code.

**Identity is never read off the wire.** ``tenant_id`` and ``principal_id``
come from ``request.state.principal``, which the app's authentication
middleware set; ``deployment_ref`` is a *selector* the route resolves against
the server's own deployment store and then checks with
``require_deployment_scope``. A body field can therefore choose which
deployment a caller means, but cannot assert that the caller may have it.

**Nothing is persisted before scope is proven.** The decision service is called
only after the deployment has been loaded and scoped, so a cross-tenant, an
unauthenticated, and an unknown-deployment request all leave the decision store
untouched. That ordering is the subject of the ``persists_no_decision_row``
tests in ``tests/service/test_enforcement_api.py``: a 404 alone would still
permit a row to have been written before the check.

**Error bodies say nothing an attacker did not already supply.** Every failure
here answers with a stable ``code`` and a fixed message from
:data:`_MESSAGES` -- never an exception's text, which for
``IdempotencyConflictError`` embeds the key and tenant, and never a driver
error, which embeds SQL. An unknown deployment and a deployment belonging to
another tenant answer identically, so the surface cannot be used to enumerate
deployment refs.

**``expected_graph_version`` comes from the deployment record.** Both read
routes source it from ``Deployment.graph_version_ref`` -- the version the
*server* deployed -- never from a submitted registration or attestation.
Deriving it from client-submitted material would let a caller satisfy its own
version check and mint ``ENFORCED`` for itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status

from zeroth.governance.attestations.heartbeat import (
    DEFAULT_STALE_AFTER_SECONDS,
    DeploymentStatusResolver,
    Heartbeat,
)
from zeroth.governance.attestations.payload import (
    RunAttestationPayload,
    SignedRunAttestation,
)
from zeroth.governance.attestations.provider import (
    PersistedCapabilityEvidenceProvider,
)
from zeroth.governance.attestations.signing import (
    attestation_digest,
    is_identical_resubmission,
    sign_attestation,
    verify_attestation,
)
from zeroth.governance.attestations.store import InventoryRegistration
from zeroth.governance.decisions.repository import IdempotencyConflictError
from zeroth.governance.decisions.request import DecisionRequest, DecisionResponse
from zeroth.governance.enforcement_wire import (
    AttestationAck,
    AttestationSubmission,
    DecisionSubmission,
    DeploymentEnforcementStatus,
    HeartbeatAck,
    HeartbeatSubmission,
    InventoryAck,
    InventorySubmission,
    RunEnforcementStatus,
)
from zeroth.governance.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.platform.primitives import utc_now
from zeroth.platform.signing import SigningKeyProvider
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)

ATTESTATION_TTL = timedelta(minutes=15)
"""How long a run-start attestation stays acceptable.

Server-assigned, and short. The window is bound into the signed payload and
enforced by
:class:`~zeroth.governance.attestations.provider.PersistedCapabilityEvidenceProvider`
against its own clock; letting a client choose it would let a run mint evidence
that never goes stale.
"""

_UNREGISTERED_COVERAGE = "unregistered"
"""Coverage recorded when a run attests before any inventory was registered.

Any token other than ``"complete"`` is incomplete to the verifying provider,
so this is fail-closed by construction. Naming the case rather than reusing
``"partial"`` keeps "nothing was ever registered" distinguishable from "a
partial inventory was registered" in the signed bytes.
"""

_UNKNOWN_CONTEXT = "enforcement_context_unknown"
_UNAVAILABLE = "enforcement_unavailable"
_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
_ATTESTATION_CONFLICT = "attestation_conflict"

_MESSAGES = {
    _UNKNOWN_CONTEXT: "enforcement context is not available for this principal",
    _UNAVAILABLE: "enforcement is not available",
    _IDEMPOTENCY_CONFLICT: "idempotency key was already used for a different action",
    # Deliberately says nothing about *which* deployment holds the winning
    # attestation, nor its digest or expiry: the caller learns that its own
    # submission did not take effect, which is all it is entitled to when the
    # attestation in force belongs to a deployment other than the one it named.
    _ATTESTATION_CONFLICT: "an attestation for this correlation is already in force",
}
"""The complete set of messages this surface will emit.

A closed table rather than formatted strings, so no code path can widen an
error body with a value that came from an exception, a driver, or a lookup.
"""


DECISION_COUNTER = "zeroth_enforcement_decisions_total"
ATTESTATION_COUNTER = "zeroth_enforcement_attestations_total"
"""Counter names for the two paths an operator cannot read off status codes.

Labels are closed vocabularies -- a decision verdict, an attestation outcome.
Tenant, deployment and correlation are deliberately *not* labels: they are
caller-supplied and unbounded, and a metrics registry that accepts unbounded
label values is a memory leak with an audit trail's job.
"""


def _fail(status_code: int, code: str) -> HTTPException:
    """Build the only kind of error this surface raises."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": _MESSAGES[code]},
    )


def _row_is_disclosable(
    row: SignedRunAttestation,
    verifier: SigningKeyProvider | None,
) -> bool:
    """Return True iff ``row``'s evidence may be reported to a caller that lost.

    Disclosure is a separate decision from classification. The caller has
    already been refused; the question here is narrower and stricter -- may it
    be told *this row's* digest and expiry, which belong to whoever wrote the
    row. Only a row this deployment can still vouch for qualifies.

    Which check applies is decided by the deployment's signing posture, never by
    the row's own columns. That distinction is the whole point: a row is
    attacker-writable state. The rewrite this gate exists to stop already proves
    that ``payload_json`` and ``digest`` can be edited together, and the same
    ``UPDATE`` can null the signature triple. Reading "unsigned" off the row
    would therefore let a signed row be *stripped* into the weaker branch, which
    moves the leak rather than closing it.

    * A deployment with a verifier vouches by signature: the row must pass
      :func:`verify_attestation`, which re-derives the digest from the payload
      *and* checks the stored signature over it. A payload rewritten with a
      recomputed digest fails, because the signature still covers the digest the
      row had when it was signed. A stripped or absent signature fails too.
    * A deployment with no verifier holds no key material and cannot vouch by
      signature for anything, so requiring one would make every conflict opaque
      and withhold the digest from ordinary unsigned retries. There the weaker
      internal-consistency check is the honest ceiling: the payload beside the
      digest must still hash to it.

    Args:
        row: The stored attestation whose evidence the 409 body would carry.
        verifier: Provider holding this deployment's verify keys, active and
            retired, or ``None`` when it holds none.

    Returns:
        True when the row's evidence may be disclosed.
    """
    if verifier is None:
        return attestation_digest(row.payload) == row.digest
    return verify_attestation(row, verifier)


def _count(bootstrap: Any, name: str, **labels: str) -> None:
    """Increment one enforcement counter, or do nothing at all.

    Observability is never allowed to change the outcome of a governed
    request: an application composed without a collector, or a collector that
    raises, must not turn an answered decision into a 500. The failure mode of
    this function is a missing data point.
    """
    collector = getattr(bootstrap, "metrics_collector", None)
    if collector is None:
        return
    try:
        collector.increment(name, labels)
    except Exception:  # a metrics fault is not a governance fault
        return


def _stale_after_seconds(bootstrap: Any) -> float:
    """Return the freshness window this deployment is configured with.

    Both status routes previously used
    :data:`~zeroth.governance.attestations.heartbeat.DEFAULT_STALE_AFTER_SECONDS`,
    so a deployment configured for 30 seconds was still reported against 90 --
    contradicting the threshold the cookbook documents as configurable. The
    configured value rides on the bootstrap; its absence (an application
    composed without the enforcement wiring) falls back to the default.
    """
    configured = getattr(bootstrap, "enforcement_stale_after_seconds", None)
    return DEFAULT_STALE_AFTER_SECONDS if configured is None else float(configured)


def _bootstrap(request: Request) -> Any:
    """Fetch the service bootstrap from app state."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)
    return bootstrap


def _component(bootstrap: Any, name: str) -> Any:
    """Fetch a wired enforcement component, or refuse the request.

    Every component is optional on the bootstrap so an application composed
    without the enforcement wiring still builds its routes. A missing one is a
    503 rather than an ``AttributeError`` surfacing as a 500 with a traceback.
    """
    component = getattr(bootstrap, name, None)
    if component is None:
        raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)
    return component


async def _scoped_deployment(request: Request, bootstrap: Any, deployment_ref: str) -> Any:
    """Resolve a submitted deployment ref to a deployment the caller may use.

    Unknown and out-of-scope answer identically. ``require_deployment_scope``
    still runs on the found branch, because it is what records the denial in
    the audit trail; its response is then replaced so that the two branches are
    indistinguishable on the wire.
    """
    service = _component(bootstrap, "deployment_service")
    try:
        deployment = await service.get(deployment_ref)
    except Exception as exc:  # storage faults must not leak
        raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc
    if deployment is None:
        raise _fail(status.HTTP_404_NOT_FOUND, _UNKNOWN_CONTEXT)
    try:
        await require_deployment_scope(request, deployment, hide_as_not_found=True)
    except HTTPException as exc:
        raise _fail(status.HTTP_404_NOT_FOUND, _UNKNOWN_CONTEXT) from exc
    return deployment


def register_enforcement_routes(app: FastAPI | APIRouter) -> None:
    """Register the tool-enforcement decision, evidence, and status routes.

    The registrars are called in declaration order, and that order is the route
    order the application -- and its OpenAPI document -- sees.
    """
    _register_decision_routes(app)
    _register_inventory_routes(app)
    _register_attestation_routes(app)
    _register_heartbeat_routes(app)
    _register_status_routes(app)


def _register_decision_routes(app: FastAPI | APIRouter) -> None:
    """Register the tool-call decision route."""

    @app.post("/enforcement/decisions", response_model=DecisionResponse)
    async def decide_tool_call(
        request: Request,
        body: DecisionSubmission,
    ) -> DecisionResponse:
        """Return the verdict for one tool call, recorded once per key.

        The idempotency key answers a repeat with the stored decision. It does
        not serialize concurrent first requests: two racing calls under one new
        key may both be evaluated, and exactly one of their decisions is
        stored and returned to both.
        """
        principal = await require_permission(request, Permission.ENFORCEMENT_REPORT)
        bootstrap = _bootstrap(request)
        deployment = await _scoped_deployment(request, bootstrap, body.deployment_ref)
        service = _component(bootstrap, "tool_decision_service")
        decision_request = DecisionRequest(
            schema_version=body.schema_version,
            tenant_id=principal.tenant_id,
            principal_id=principal.subject,
            deployment_ref=deployment.deployment_ref,
            action=body.action,
            idempotency_key=body.idempotency_key,
            policy_bindings=body.policy_bindings,
        )
        try:
            decision = await service.decide(decision_request)
        except IdempotencyConflictError as exc:
            # A conflict is the caller's own doing and is never retryable: the
            # key already answers for a different action. 409, and nothing of
            # the exception's text, which names the key and the tenant.
            _count(bootstrap, DECISION_COUNTER, outcome="conflict")
            raise _fail(status.HTTP_409_CONFLICT, _IDEMPOTENCY_CONFLICT) from exc
        except Exception as exc:  # a decision outage is not an allow
            # Counted separately from every verdict: an outage denies, but a
            # deployment that is denying because it cannot evaluate is not the
            # same operational fact as one that is denying on policy.
            _count(bootstrap, DECISION_COUNTER, outcome="unavailable")
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc
        _count(bootstrap, DECISION_COUNTER, outcome="decided", verdict=decision.kind.value)
        return decision


def _register_inventory_routes(app: FastAPI | APIRouter) -> None:
    """Register the declared tool-inventory registration route."""

    @app.post(
        "/enforcement/registrations",
        response_model=InventoryAck,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_tool_inventory(
        request: Request,
        body: InventorySubmission,
    ) -> InventoryAck:
        """Record the tool inventory an adapter declares for one deployment."""
        principal = await require_permission(request, Permission.ENFORCEMENT_REPORT)
        bootstrap = _bootstrap(request)
        deployment = await _scoped_deployment(request, bootstrap, body.deployment_ref)
        repository = _component(bootstrap, "inventory_registration_repository")
        # ``inventory_fingerprint`` and ``tool_count`` are not passed and
        # cannot be: the model derives both from ``tools``. That is what stops
        # a caller supplying both sides of the provider's comparison.
        registration = InventoryRegistration(
            tenant_id=principal.tenant_id,
            deployment_ref=deployment.deployment_ref,
            graph_version=body.graph_version,
            adapter_version=body.adapter_version,
            coverage=body.coverage,
            tools=body.tools,
        )
        try:
            stored = await repository.register(registration)
        except Exception as exc:  # storage faults must not leak
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc
        return InventoryAck(
            registration_id=stored.registration_id,
            deployment_ref=stored.deployment_ref,
            registered_at=stored.registered_at,
        )


def _register_attestation_routes(app: FastAPI | APIRouter) -> None:
    """Register the run-start attestation route."""

    @app.post(
        "/enforcement/attestations",
        response_model=AttestationAck,
        status_code=status.HTTP_201_CREATED,
    )
    async def attest_run_start(
        request: Request,
        body: AttestationSubmission,
        response: Response,
    ) -> AttestationAck:
        """Sign and record a run's start-of-run claims, if the run has none yet.

        A run's evidence is fixed by the first attestation the server accepts.
        A submission that loses to an earlier one answers 409 and reports
        ``authoritative: false`` together with the digest of the attestation
        actually in force -- never a bare 200, which would tell the client its
        own claims are what the run will be judged on.

        **The winner is disclosed only when it belongs to the deployment the
        caller named.** Correlations are unique per tenant, not per deployment,
        so a loser may have lost to a sibling deployment's run; that case
        answers a fixed, opaque 409 instead, because the alternative is an
        oracle for reading another deployment's digest and expiry.

        **A retry of the attestation already in force is not a second
        attestation.** The server stamps ``issued_at`` and ``expires_at`` on
        arrival, so an adapter that lost its response cannot resubmit the bytes
        it sent -- the digest and signature differ on every attempt, and an
        envelope comparison alone would tell an honest retry that some other
        attestation governs its run. ``is_identical_resubmission`` decides that
        case on the claims plus a re-signature of the stored payload; when it
        holds, the caller gets the *original* acceptance back. Changed claims,
        a rotated key, and a signed/unsigned change all still answer 409.

        **A losing insert is not always a lost race.** Two concurrent identical
        requests stamped with the same ``issued_at`` build the same payload, so
        deterministic signing gives the loser an envelope equal to the winner's:
        a duplicate, not a conflict, and counting it as ``recorded`` would
        overstate how many attestations were stored. The branch is chosen on
        ``record``'s ``inserted`` -- did *this* write create the row -- and
        every non-insert takes the same path: read the winner back
        deployment-scoped, then classify with ``is_identical_resubmission``.

        **``record``'s own ``authoritative`` is deliberately not consulted
        here.** It is computed from the stored envelope *columns*, which are no
        evidence that the stored ``payload_json`` derives from them, nor that
        the row's flat ``deployment_ref`` matches the deployment named inside
        its signed payload. Short-circuiting on it -- as an earlier revision
        did -- let an internally inconsistent row answer 201 for claims it does
        not carry, and skipped the scoped read that keeps a sibling
        deployment's evidence unreadable. Re-signing the stored payload
        recomputes its digest *from that payload*, so an inconsistent row fails
        the comparison rather than passing it, and the scoped read is now on
        every non-insert path rather than only on the slow one.

        **What that costs: a 503 window this path did not previously have.**
        The read happens *after* ``record`` has returned, and only on the
        branch where this submission did **not** insert -- so a storage fault
        between the two turns what would have been a 201 for an exact-envelope
        duplicate into a fail-closed 503. Nothing is lost by that: this request
        wrote nothing (it lost the insert), and the row it would have been
        answered about is the winner's, already durably in place. A retry reads
        that winner back and answers 201 as before. This is the intended
        direction -- a row that cannot be read is no evidence, so it must not
        be certified -- but it is a real behaviour change for a case that used
        to answer without reading, and it is stated here rather than left for
        someone to discover from a metric.

        **The retry answer is byte-identical to the first one** (201,
        ``authoritative: true``, the stored digest and the stored expiry): a
        client that cannot tell a retry from the original is what idempotency
        means, and the window reported is the one actually in force rather than
        a fresh one the run does not have. Only the metric separates them, as
        ``already_recorded`` -- nothing on the wire is false, and an operator
        still gets to see retry volume instead of it inflating ``recorded``.
        """
        principal = await require_permission(request, Permission.ENFORCEMENT_REPORT)
        bootstrap = _bootstrap(request)
        deployment = await _scoped_deployment(request, bootstrap, body.deployment_ref)
        repository = _component(bootstrap, "run_attestation_repository")
        registrations = _component(bootstrap, "inventory_registration_repository")
        try:
            registered = await registrations.latest_for_deployment(
                principal.tenant_id,
                deployment.deployment_ref,
            )
        except Exception as exc:  # storage faults must not leak
            _count(bootstrap, ATTESTATION_COUNTER, outcome="unavailable")
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc
        signer = getattr(bootstrap, "signer", None)
        # Classification stays on the CURRENT signer -- a retry is "what this
        # deployment would sign today". Disclosure uses the verifier, which also
        # holds retired keys, because a row signed before a rotation is still
        # this deployment's evidence.
        verifier = getattr(bootstrap, "verifier", None)
        issued_at = utc_now()
        signed = sign_attestation(
            RunAttestationPayload(
                correlation_id=body.correlation_id,
                tenant_id=principal.tenant_id,
                deployment_ref=deployment.deployment_ref,
                graph_version=body.graph_version,
                adapter_version=body.adapter_version,
                inventory_fingerprint=body.inventory_fingerprint,
                # Coverage and count come from the deployment's stored
                # registration, never from this body. They are pure
                # self-certification -- a client asserting "complete" and "999"
                # about its own inventory -- and the audited attack was exactly
                # that assertion going unchallenged into the signed bytes. The
                # *fingerprint* above stays client-submitted on purpose: it is
                # the run's binding to a tool set, and the provider's check is
                # that it equals the digest the server recomputed. Deriving it
                # here too would compare a value with itself.
                inventory_coverage=(
                    _UNREGISTERED_COVERAGE if registered is None else registered.coverage
                ),
                tool_count=0 if registered is None else registered.tool_count,
                claimed_level=body.claimed_level,
                issued_at=issued_at,
                expires_at=issued_at + ATTESTATION_TTL,
            ),
            signer,
        )
        try:
            written = await repository.record(signed)
            # Scoped to *this* deployment. Uniqueness is tenant-wide, so a
            # correlation another deployment already attested makes this
            # submission lose; reading the winner back unscoped -- as an
            # earlier revision did -- turned the 409 body into an oracle over
            # a sibling deployment's runs, since deployment scope is
            # tenant-and-workspace wide and any principal here may name any of
            # them. ``None`` therefore means "the winner is not ours".
            stored = (
                None
                if written.inserted
                else await repository.find_for_deployment(
                    principal.tenant_id,
                    deployment.deployment_ref,
                    body.correlation_id,
                )
            )
        except Exception as exc:  # storage faults must not leak
            _count(bootstrap, ATTESTATION_COUNTER, outcome="unavailable")
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc
        if not written.inserted and stored is None:
            # Raised outside the block above on purpose: ``HTTPException`` is
            # an ``Exception``, so raising it inside would be caught and
            # reported as a storage outage.
            #
            # The alternative fix -- scoping the UNIQUE constraint by
            # deployment -- was rejected: it would let two deployments both
            # hold an authoritative attestation for one correlation, which
            # weakens "a run's evidence is fixed by the first attestation the
            # server accepts" rather than defending it.
            _count(
                bootstrap,
                ATTESTATION_COUNTER,
                outcome="earlier_attestation_in_force",
            )
            raise _fail(status.HTTP_409_CONFLICT, _ATTESTATION_CONFLICT)
        # The attestation the run is judged on: this submission when the insert
        # won, otherwise the row that beat it.
        in_force = signed if written.inserted else stored
        if written.inserted:
            authoritative = True
            outcome = "recorded"
        elif is_identical_resubmission(signed, in_force, signer):
            # The ordinary retry: a fresh ``issued_at`` made the envelope
            # differ, but the claims and the signer did not. The same
            # attestation, restated.
            authoritative = True
            outcome = "already_recorded"
        else:
            authoritative = False
            outcome = "earlier_attestation_in_force"
        _count(bootstrap, ATTESTATION_COUNTER, outcome=outcome)
        if not authoritative and not _row_is_disclosable(in_force, verifier):
            # Disclosure, not classification: the branch above already refused
            # this row, but the body below would still report its digest and
            # expiry. The scoped read re-derives all three identities from the
            # signed payload, so a row that names another deployment now reads
            # as absent -- but a payload rewritten to name *this* deployment
            # passes that read, and its envelope columns still carry the
            # original row's evidence. That is the leak: the caller learns
            # evidence about an attestation it does not own.
            #
            # Re-deriving the digest is not enough to close it. That check asks
            # only whether the payload still hashes to the digest beside it, and
            # a rewrite that edits both together answers yes -- which is exactly
            # what a migration re-homing deployments produces, and what an
            # attacker with write access produces deliberately. The signature is
            # the part such a rewrite cannot reproduce, because it covers the
            # digest the row had when it was signed.
            #
            # So a deployment that can verify must verify, and
            # ``_row_is_disclosable`` decides which check applies from the
            # deployment's signing posture rather than from the row's own
            # columns -- see its docstring for why that distinction is what
            # keeps a stripped signature from selecting the weaker branch. The
            # verifier retains rotated-away keys, so the legitimate rotated-key
            # retry that
            # ``test_an_identical_attestation_retry_under_a_rotated_key_is_refused``
            # pins still discloses; a deployment holding no keys at all keeps
            # the unsigned contract.
            #
            # Only ``not authoritative`` needs the gate: the two authoritative
            # paths are this request's own submission, or a row
            # ``is_identical_resubmission`` already re-derived. ``in_force`` is
            # therefore always ``stored`` here, and a missing ``stored`` was
            # answered opaquely above.
            raise _fail(status.HTTP_409_CONFLICT, _ATTESTATION_CONFLICT)
        if not authoritative:
            response.status_code = status.HTTP_409_CONFLICT
        return AttestationAck(
            correlation_id=body.correlation_id,
            authoritative=authoritative,
            status="recorded" if authoritative else "earlier_attestation_in_force",
            digest=in_force.digest,
            expires_at=in_force.payload.expires_at,
        )


def _register_heartbeat_routes(app: FastAPI | APIRouter) -> None:
    """Register the deployment liveness heartbeat route."""

    @app.post(
        "/enforcement/heartbeats",
        response_model=HeartbeatAck,
        status_code=status.HTTP_201_CREATED,
    )
    async def report_heartbeat(
        request: Request,
        body: HeartbeatSubmission,
    ) -> HeartbeatAck:
        """Append one liveness ping for a deployment."""
        principal = await require_permission(request, Permission.ENFORCEMENT_REPORT)
        bootstrap = _bootstrap(request)
        deployment = await _scoped_deployment(request, bootstrap, body.deployment_ref)
        repository = _component(bootstrap, "enforcement_heartbeat_repository")
        heartbeat = Heartbeat(
            tenant_id=principal.tenant_id,
            deployment_ref=deployment.deployment_ref,
            graph_version=body.graph_version,
            adapter_version=body.adapter_version,
            reported_level=body.reported_level,
        )
        try:
            stored = await repository.record(heartbeat)
        except Exception as exc:  # storage faults must not leak
            raise _fail(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE) from exc
        return HeartbeatAck(
            heartbeat_id=stored.heartbeat_id,
            deployment_ref=stored.deployment_ref,
            observed_at=stored.observed_at,
        )


def _register_status_routes(app: FastAPI | APIRouter) -> None:
    """Register the deployment- and run-level enforcement status reads."""

    @app.get(
        "/enforcement/deployments/{deployment_ref}/status",
        response_model=DeploymentEnforcementStatus,
    )
    async def read_deployment_status(
        request: Request,
        deployment_ref: str,
    ) -> DeploymentEnforcementStatus:
        """Report a deployment's last-known level from its newest heartbeat.

        The level is run through :class:`CapabilityReporter` rather than read
        off the heartbeat, so the staleness window and the graph-version match
        are applied by the same code that governs the gateway. A heartbeat can
        never reach ``ENFORCED``: it proves no tool inventory.
        """
        principal = await require_permission(request, Permission.ENFORCEMENT_REPORT)
        bootstrap = _bootstrap(request)
        deployment = await _scoped_deployment(request, bootstrap, deployment_ref)
        repository = _component(bootstrap, "enforcement_heartbeat_repository")
        resolver = DeploymentStatusResolver(
            heartbeats=repository,
            tenant_id=principal.tenant_id,
            deployment_ref=deployment.deployment_ref,
            stale_after_seconds=_stale_after_seconds(bootstrap),
        )
        evidence = await resolver.last_known_evidence()
        reporter = CapabilityReporter(
            stale_after_seconds=resolver.stale_after_seconds,
            expected_graph_version=deployment.graph_version_ref,
        )
        return DeploymentEnforcementStatus(
            deployment_ref=deployment.deployment_ref,
            governance_level=reporter.level_for_deployment(evidence).value,
            observed_at=None if evidence is None else evidence.observed_at,
            stale_after_seconds=resolver.stale_after_seconds,
        )

    @app.get(
        "/enforcement/deployments/{deployment_ref}/runs/{correlation_id}",
        response_model=RunEnforcementStatus,
    )
    async def read_run_status(
        request: Request,
        deployment_ref: str,
        correlation_id: str,
    ) -> RunEnforcementStatus:
        """Report the level the server can prove for one attested run.

        The correlation is attacker-suppliable, so the provider is bound to the
        caller's tenant and to the resolved deployment: a correlation stored
        under another tenant reads as absent, never as that tenant's evidence.
        """
        principal = await require_permission(request, Permission.ENFORCEMENT_REPORT)
        bootstrap = _bootstrap(request)
        deployment = await _scoped_deployment(request, bootstrap, deployment_ref)
        provider = PersistedCapabilityEvidenceProvider(
            attestations=_component(bootstrap, "run_attestation_repository"),
            registrations=_component(bootstrap, "inventory_registration_repository"),
            signer=getattr(bootstrap, "signer", None),
            tenant_id=principal.tenant_id,
            deployment_ref=deployment.deployment_ref,
            expected_graph_version=deployment.graph_version_ref,
        )
        reporter = CapabilityReporter(
            provider,
            stale_after_seconds=_stale_after_seconds(bootstrap),
            expected_graph_version=deployment.graph_version_ref,
        )
        level = await reporter.level_for_run(correlation_id)
        return RunEnforcementStatus(
            correlation_id=correlation_id,
            governance_level=level.value,
        )
