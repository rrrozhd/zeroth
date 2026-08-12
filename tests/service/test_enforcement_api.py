"""ZER-8 enforcement API: scope, idempotency, evidence, and error opacity.

Three of these tests assert a *count*, not a status code. A route that answered
404 after already writing a decision row would satisfy an assertion on the
response alone while leaving one tenant's decision recorded under another
tenant's request, so the persistence claim is checked directly against
``decision_records``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.service.helpers import (
    agent_graph,
    api_key_headers,
    deploy_service,
    scoped_auth_config,
)
from zeroth.governance.attestations.inventory import (
    RegisteredTool,
    recompute_inventory_fingerprint,
)
from zeroth.governance.attestations.payload import RunAttestationPayload, stable_claims
from zeroth.governance.attestations.signing import (
    attestation_digest,
    sign_attestation,
)
from zeroth.governance.attestations.store import (
    InventoryRegistrationRepository,
    RunAttestationRepository,
)
from zeroth.governance.identity import ServiceRole
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.bootstrap.factory import bootstrap_scoped_app as bootstrap_app

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
SECRET_A = "enforcement-key-a"
REVIEWER_SECRET = "enforcement-reviewer-key"

_LEAKY_FRAGMENTS = (
    "traceback",
    "sqlite",
    "select ",
    "insert ",
    "decision_records",
    "idempotencyconflicterror",
    "valueerror",
    ".py",
    "/users",
    "src/zeroth",
)


def _auth_config():
    return scoped_auth_config(
        ("operator-a", SECRET_A, ServiceRole.OPERATOR, TENANT_A, None),
    )


def _action(*, arguments_digest: str = "sha256:aaa", side_effect: str = "unknown") -> dict:
    return {
        "name": "search",
        "fingerprint": "sha256:fingerprint",
        "arguments_digest": arguments_digest,
        "side_effect": side_effect,
    }


async def _decision_row_count(database) -> int:
    """Total rows in ``decision_records``, across every tenant."""
    async with database.transaction() as connection:
        row = await connection.fetch_one("SELECT COUNT(*) AS total FROM decision_records")
    return int(row["total"])


async def _two_tenant_app(sqlite_db, *, auth_config=None):
    """Serve tenant A's deployment while tenant B owns a second one."""
    _, deployment_a = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-enforcement-a"),
        deployment_ref="enforcement-a",
        tenant_id=TENANT_A,
        auth_config=auth_config or _auth_config(),
    )
    _, deployment_b = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-enforcement-b"),
        deployment_ref="enforcement-b",
        tenant_id=TENANT_B,
        auth_config=auth_config or _auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment_a.deployment_ref,
        tenant_id=TENANT_A,
        auth_config=auth_config or _auth_config(),
    )
    return app, deployment_a, deployment_b


async def _single_tenant_app(sqlite_db, *, auth_config=None):
    _, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-enforcement-solo"),
        deployment_ref="enforcement-solo",
        tenant_id=TENANT_A,
        auth_config=auth_config or _auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        tenant_id=TENANT_A,
        auth_config=auth_config or _auth_config(),
    )
    return app, deployment


def _decision_body(deployment_ref: str, *, key: str, **action_overrides) -> dict:
    return {
        "deployment_ref": deployment_ref,
        "action": _action(**action_overrides),
        "idempotency_key": key,
    }


async def test_a_cross_tenant_decision_request_persists_no_decision_row(sqlite_db) -> None:
    """Tenant A aiming at tenant B's deployment is refused before anything is written."""
    app, _, deployment_b = await _two_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(deployment_b.deployment_ref, key="cross-tenant"),
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "enforcement_context_unknown"
    assert await _decision_row_count(sqlite_db) == 0


async def test_enforcement_report_denied_for_reviewer_without_permission(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("reviewer-a", REVIEWER_SECRET, ServiceRole.REVIEWER, TENANT_A, None),
    )
    app, deployment = await _single_tenant_app(sqlite_db, auth_config=auth_config)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(deployment.deployment_ref, key="reviewer-denied"),
            headers=api_key_headers(REVIEWER_SECRET),
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert await _decision_row_count(sqlite_db) == 0


async def test_an_expired_credential_persists_no_decision_row(sqlite_db) -> None:
    """An expired bearer token is rejected by the middleware, so no row is written."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "enforcement-key"
    from zeroth.service.api.authentication import BearerTokenConfig, ServiceAuthConfig

    auth_config = ServiceAuthConfig(
        bearer=BearerTokenConfig(
            issuer="https://issuer.example.test",
            audience="zeroth-service",
            jwks={"keys": [jwk]},
        )
    )
    expired = jwt.encode(
        {
            "sub": "adapter",
            "roles": [ServiceRole.OPERATOR.value],
            "tenant_id": TENANT_A,
            "iss": "https://issuer.example.test",
            "aud": "zeroth-service",
            "exp": int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "enforcement-key"},
    )
    app, deployment = await _single_tenant_app(sqlite_db, auth_config=auth_config)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(deployment.deployment_ref, key="expired"),
            headers={"Authorization": f"Bearer {expired}"},
        )

    assert response.status_code == 401
    assert await _decision_row_count(sqlite_db) == 0


async def test_an_unknown_deployment_persists_no_decision_row(sqlite_db) -> None:
    """A deployment ref nobody deployed is refused before the decision service runs."""
    app, _ = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body("no-such-deployment", key="unknown-deployment"),
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "enforcement_context_unknown"
    assert await _decision_row_count(sqlite_db) == 0


async def test_a_replayed_idempotency_key_with_a_different_action_returns_409(
    sqlite_db,
) -> None:
    """A key that already answers for one action must never answer for another."""
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        first = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(deployment.deployment_ref, key="shared-key"),
            headers=api_key_headers(SECRET_A),
        )
        second = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(
                deployment.deployment_ref,
                key="shared-key",
                arguments_digest="sha256:bbb",
            ),
            headers=api_key_headers(SECRET_A),
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_conflict"


async def test_a_replayed_idempotency_key_with_the_same_action_returns_the_original_decision(
    sqlite_db,
) -> None:
    """A replay re-serves the recorded decision rather than minting a second one."""
    app, deployment = await _single_tenant_app(sqlite_db)
    body = _decision_body(deployment.deployment_ref, key="replayed-key")

    with TestClient(app) as client:
        first = client.post(
            "/v1/enforcement/decisions",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        second = client.post(
            "/v1/enforcement/decisions",
            json=body,
            headers=api_key_headers(SECRET_A),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["decision_id"] == first.json()["decision_id"]
    assert second.json()["issued_at"] == first.json()["issued_at"]
    assert await _decision_row_count(sqlite_db) == 1


async def test_an_error_response_leaks_no_internal_detail(sqlite_db) -> None:
    """Every refusal carries a stable code and a fixed message, and nothing else."""
    app, deployment = await _single_tenant_app(sqlite_db)
    body = _decision_body(deployment.deployment_ref, key="leak-probe")

    with TestClient(app) as client:
        client.post(
            "/v1/enforcement/decisions",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        conflict = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(
                deployment.deployment_ref,
                key="leak-probe",
                arguments_digest="sha256:ccc",
            ),
            headers=api_key_headers(SECRET_A),
        )
        unknown = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body("no-such-deployment", key="leak-probe-2"),
            headers=api_key_headers(SECRET_A),
        )

    for response in (conflict, unknown):
        payload = response.json()
        assert set(payload) == {"detail"}
        assert set(payload["detail"]) == {"code", "message"}
        text = response.text.lower()
        for fragment in _LEAKY_FRAGMENTS:
            assert fragment not in text, f"{fragment!r} leaked into {response.text!r}"
    # The key itself is client-supplied, but it must not be echoed back either:
    # a body that quotes the request is one refactor away from quoting the
    # exception that carries it.
    assert "leak-probe" not in conflict.text


async def test_a_registration_records_versions_fingerprint_and_coverage(sqlite_db) -> None:
    """A posted inventory is readable through the store with every field intact.

    The body carries tool *identities* and no digest or count: since audit
    round 1 the server recomputes both, so a caller cannot declare an
    inventory digest its own declared tool set does not produce.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    tools = (
        RegisteredTool(name="search", fingerprint="fp-search"),
        RegisteredTool(name="write", fingerprint="fp-write"),
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/registrations",
            json={
                "deployment_ref": deployment.deployment_ref,
                "graph_version": "graph-enforcement-solo@1",
                "adapter_version": "1.0",
                "coverage": "complete",
                "tools": [
                    {"name": "search", "fingerprint": "fp-search"},
                    {"name": "write", "fingerprint": "fp-write"},
                ],
            },
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 201
    stored = await InventoryRegistrationRepository(sqlite_db).latest_for_deployment(
        TENANT_A,
        deployment.deployment_ref,
    )
    assert stored is not None
    assert stored.registration_id == response.json()["registration_id"]
    assert stored.graph_version == "graph-enforcement-solo@1"
    assert stored.adapter_version == "1.0"
    assert stored.coverage == "complete"
    assert stored.tool_count == 2
    assert stored.tools == tools
    assert stored.inventory_fingerprint == recompute_inventory_fingerprint(tools)


async def test_http_decision_cannot_downgrade_registered_approval(sqlite_db) -> None:
    """The production route refuses a request weaker than its stored descriptor."""
    app, deployment = await _single_tenant_app(sqlite_db)
    registration = {
        "deployment_ref": deployment.deployment_ref,
        "graph_version": "graph-enforcement-solo@1",
        "adapter_version": "2.0",
        "coverage": "complete",
        "tools": [
            {
                "name": "search",
                "fingerprint": "sha256:fingerprint",
                "side_effect": "read_only",
                "requires_approval": True,
            }
        ],
    }
    decision = _decision_body(
        deployment.deployment_ref,
        key="approval-downgrade",
        side_effect="read_only",
    )
    decision["action"]["requires_approval"] = False

    with TestClient(app) as client:
        registered = client.post(
            "/v1/enforcement/registrations",
            json=registration,
            headers=api_key_headers(SECRET_A),
        )
        response = client.post(
            "/v1/enforcement/decisions",
            json=decision,
            headers=api_key_headers(SECRET_A),
        )

    assert registered.status_code == 201
    assert response.status_code == 200
    assert response.json()["kind"] == "deny"
    assert response.json()["reason_code"] == "capability_denied"


async def test_a_registration_cannot_declare_its_own_digest_or_count(sqlite_db) -> None:
    """R8: the self-certifying fields are not merely ignored -- they are gone.

    ``extra="forbid"`` on the submission means the audited request body is now
    a 422 rather than a silently corrected registration, so the attack cannot
    even be expressed on the wire.
    """
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/registrations",
            json={
                "deployment_ref": deployment.deployment_ref,
                "graph_version": "graph-enforcement-solo@1",
                "adapter_version": "1.0",
                "coverage": "complete",
                "tools": [],
                "inventory_fingerprint": "X",
                "tool_count": 999,
            },
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 422


async def test_a_registration_naming_one_tool_twice_is_refused_as_a_bad_request(
    sqlite_db,
) -> None:
    """H1: a repeated tool name is a 422, and nothing is registered.

    Registering ``("send_email", "fp-impostor")`` beside the genuine
    ``("send_email", "fp-legit")`` would authorize both callables under one
    governed label while leaving coverage ``complete`` -- the impostor admitted
    by standing next to the tool it impersonates.

    **The status code is the assertion, not an incidental detail.**
    ``InventoryRegistration`` also refuses the shape, but the route builds it
    inside the handler body, where a pydantic ``ValidationError`` is an
    unhandled exception and therefore a 500 -- a server fault reported for a
    malformed client request. The rule is carried on ``InventorySubmission`` so
    the refusal happens at the transport boundary instead.
    """
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/registrations",
            json={
                "deployment_ref": deployment.deployment_ref,
                "graph_version": "graph-enforcement-solo@1",
                "adapter_version": "1.0",
                "coverage": "complete",
                "tools": [
                    {"name": "send_email", "fingerprint": "fp-legit"},
                    {"name": "send_email", "fingerprint": "fp-impostor"},
                ],
            },
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 422
    assert (
        await InventoryRegistrationRepository(sqlite_db).latest_for_deployment(
            TENANT_A,
            deployment.deployment_ref,
        )
        is None
    ), "a refused registration must leave no inventory behind"


async def test_a_registration_repeating_an_identical_tool_is_refused_too(
    sqlite_db,
) -> None:
    """H1: the duplicate rule is on the name, not on the pair.

    Two byte-identical entries carry no impostor, but the digest recomputes
    over both and the declaration then no longer describes the adapter's tool
    set. Refused for the same reason and with the same status.
    """
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/registrations",
            json={
                "deployment_ref": deployment.deployment_ref,
                "graph_version": "graph-enforcement-solo@1",
                "adapter_version": "1.0",
                "coverage": "complete",
                "tools": [
                    {"name": "search", "fingerprint": "fp-search"},
                    {"name": "search", "fingerprint": "fp-search"},
                ],
            },
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 422


def _attestation_body(deployment_ref: str, *, fingerprint: str) -> dict:
    """Build an attestation body.

    ``inventory_coverage`` and ``tool_count`` are absent: since audit round 1
    the server takes both from the deployment's stored registration, so they
    are no longer things a run can assert about itself. The fingerprint stays
    client-supplied -- it binds the run to a tool set, and the provider's check
    is that it equals the digest the server recomputed.
    """
    return {
        "correlation_id": "run-correlation",
        "deployment_ref": deployment_ref,
        "graph_version": "graph-enforcement-solo@1",
        "adapter_version": "1.0",
        "inventory_fingerprint": fingerprint,
        "claimed_level": "enforced",
    }


async def test_an_attestation_that_loses_to_an_earlier_one_is_reported_as_not_authoritative(
    sqlite_db,
) -> None:
    """First write wins per run, and the loser is told so instead of getting a bare 200."""
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        first = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment.deployment_ref, fingerprint="sha256:first"),
            headers=api_key_headers(SECRET_A),
        )
        second = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment.deployment_ref, fingerprint="sha256:second"),
            headers=api_key_headers(SECRET_A),
        )

    assert first.status_code == 201
    assert first.json()["authoritative"] is True
    assert second.status_code == 409
    assert second.json()["authoritative"] is False
    assert second.json()["status"] == "earlier_attestation_in_force"
    # The digest reported to the loser describes the attestation actually in
    # force, which is the first one -- not the claims it just submitted.
    assert second.json()["digest"] == first.json()["digest"]

    stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
        TENANT_A,
        "run-correlation",
    )
    assert stored is not None
    assert stored.payload.inventory_fingerprint == "sha256:first"
    # No inventory was ever registered for this deployment, so the coverage and
    # count bound into the signed bytes are the server's "unregistered", not a
    # claim the run made about itself.
    assert stored.payload.inventory_coverage == "unregistered"
    assert stored.payload.tool_count == 0


async def _attestation_row_count(database) -> int:
    """Total rows in ``run_attestations``, across every tenant."""
    async with database.transaction() as connection:
        row = await connection.fetch_one("SELECT COUNT(*) AS total FROM run_attestations")
    return int(row["total"])


def _wire_signer(app, key_id: str, secret: bytes) -> None:
    """Give the running app a real signing key, and retain the previous one.

    ``bootstrap_app`` builds a signer-less surface in tests -- no key is
    configured in the process env -- and an unsigned attestation would make the
    re-signature half of the retry check pass trivially, since both sides would
    be ``None``. The tests below inject a key so the envelope comparison is
    actually exercised, and so rotating it is expressible.

    Signer and verifier are wired separately because they have different
    lifetimes in production, and calling this twice is what a rotation looks
    like here. The signer is REPLACED, so it only ever signs under the newest
    key. The verifier ACCUMULATES, mirroring ``provenance.retired_keys_json``:
    a real deployment lists the keys it rotated away from, and rows signed
    under them stay verifiable. Without that the rotation would retroactively
    unverify every earlier row.
    """
    bootstrap = app.state.bootstrap
    bootstrap.signer = EnvHmacSigner(key_id=key_id, keys={key_id: secret})
    retained = dict(getattr(getattr(bootstrap, "verifier", None), "_keys", {}))
    retained[key_id] = secret
    bootstrap.verifier = EnvHmacSigner(key_id=key_id, keys=retained)


async def test_an_identical_attestation_retry_returns_the_original_acceptance(
    sqlite_db,
) -> None:
    """An adapter that lost its response and retried is not told it lost.

    The server stamps ``issued_at``/``expires_at`` on arrival, so the retry's
    payload, digest and signature all differ from the stored ones and the
    envelope comparison in ``RunAttestationRepository.record`` refuses it. That
    made the route answer 409 to a client whose attestation *is* the one in
    force -- the run's evidence and the caller's claims were identical, and it
    was told they were not.

    The retry now gets the first response back verbatim, and writes nothing.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        body = _attestation_body(deployment.deployment_ref, fingerprint="sha256:retried")
        first = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        second = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )

    assert first.status_code == 201
    assert first.json()["authoritative"] is True
    # Byte-identical, expiry included: the window reported is the one actually
    # in force, not a fresh one the run does not have.
    assert second.status_code == 201
    assert second.json() == first.json()
    # Idempotent, not merely tolerant -- the retry left no second row behind.
    assert await _attestation_row_count(sqlite_db) == 1

    stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
        TENANT_A,
        "run-correlation",
    )
    assert stored is not None
    assert stored.digest == first.json()["digest"]
    assert stored.signature is not None


async def test_an_identical_attestation_retry_under_a_rotated_key_is_refused(
    sqlite_db,
) -> None:
    """Same claims, different key: still 409, because the evidence changed.

    The claims projection matches here -- only the signing key moved -- so this
    is the case that proves the retry check is not claims-only. Re-signing the
    stored payload under the new key yields a different envelope, and the
    caller is told the truth: the attestation in force is not the one it would
    have produced.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        body = _attestation_body(deployment.deployment_ref, fingerprint="sha256:rotated")
        first = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        _wire_signer(app, "attest-k2", b"enforcement-attestation-key-rotated")
        second = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["authoritative"] is False
    assert second.json()["status"] == "earlier_attestation_in_force"
    assert second.json()["digest"] == first.json()["digest"]
    assert await _attestation_row_count(sqlite_db) == 1


async def test_a_stored_row_whose_payload_was_swapped_is_refused_not_certified(
    sqlite_db,
) -> None:
    """An envelope-identical row is not proof its *payload* is the caller's claims.

    ``record`` decides ``authoritative`` from the stored envelope columns --
    digest, signature, key id, algorithm. It does not recompute the digest over
    ``payload_json``. For a row this repository wrote the two agree, so an
    earlier revision short-circuited on that flag and answered 201 without
    reading the row back at all.

    That is the defect here. This test stores a row by ``UPDATE`` whose payload
    no longer matches its envelope columns -- the shape a bug elsewhere, or an
    older revision, could leave behind -- and posts a body whose freshly built
    envelope equals those columns exactly. The short-circuit certified it. The
    route must instead re-derive from the stored payload and refuse: the claims
    in force are not the claims submitted.

    The refusal is *opaque*. A digest that no longer derives from the payload
    it is stored beside describes bytes the row does not carry, so reporting it
    back would answer the caller with evidence about an attestation that no
    longer exists in that form.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")
    frozen = datetime(2026, 7, 31, 9, 0, 0, 500000, tzinfo=UTC)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("zeroth.service.api.enforcement_api.utc_now", lambda: frozen)
        body = _attestation_body(deployment.deployment_ref, fingerprint="sha256:honest")

        with TestClient(app) as client:
            first = client.post(
                "/v1/enforcement/attestations",
                json=body,
                headers=api_key_headers(SECRET_A),
            )
            assert first.status_code == 201

            # Swap the stored claims, leaving the envelope columns intact. The
            # row now certifies bytes it does not carry.
            stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
                TENANT_A,
                "run-correlation",
            )
            assert stored is not None
            swapped = stored.payload.model_copy(update={"claimed_level": "observed"})
            async with sqlite_db.transaction(write_lock=True) as connection:
                await connection.execute(
                    "UPDATE run_attestations SET payload_json = ?",
                    (swapped.model_dump_json(),),
                )

            # Same body, same frozen clock: the submitted envelope is exactly
            # the one still in the row's columns.
            replayed = client.post(
                "/v1/enforcement/attestations",
                json=body,
                headers=api_key_headers(SECRET_A),
            )

    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "attestation_conflict"
    # The stale envelope is not reported back: neither its digest nor its expiry.
    assert first.json()["digest"] not in replayed.text
    assert str(first.json()["expires_at"]) not in replayed.text


async def test_a_stored_row_whose_issuance_window_was_edited_is_refused(
    sqlite_db,
) -> None:
    """The re-signature half must re-derive the digest, and this test pins that.

    ``is_identical_resubmission`` runs two checks in order: claims equality
    first, then re-signing the stored payload. This test is not the only one
    that *reaches* the re-signature -- the rotated-key test above passes claims
    equality and gets there too. Across the two attestation suites -- this file
    and ``tests/governance/test_run_attestations.py`` -- it is the only test
    that distinguishes re-deriving the digest from ``payload_json`` from merely
    signing the stored ``digest`` column, which is the property the digest half
    exists to provide. A rotated key changes the signature either way, so that
    test never told the two apart; the sibling test above swaps
    ``claimed_level``, which ``stable_claims`` covers, so it is rejected at the
    first check and never reaches the second at all. Measured rather than
    assumed, and scoped to what was measured: under a mutant that signs the
    stored digest column, this was the one test of those two suites that
    failed (M7). Nothing was instrumented beyond them.

    This edits only ``issued_at`` and ``expires_at`` -- the *only* two fields
    the projection drops -- and leaves every envelope column untouched. The
    claims therefore still match, so the re-signature is the only thing left
    that can refuse: it recomputes the digest over the edited payload and finds
    it disagrees with the stored digest column. A row whose signed window no
    longer matches the bytes that were signed is not the caller's attestation
    restated, and must not be answered as one -- nor may its stale digest be
    reported back, so the refusal is the fixed opaque conflict.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        body = _attestation_body(deployment.deployment_ref, fingerprint="sha256:honest")
        first = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        assert first.status_code == 201

        stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
            TENANT_A,
            "run-correlation",
        )
        assert stored is not None
        drifted = stored.payload.model_copy(
            update={
                "issued_at": stored.payload.issued_at + timedelta(days=365),
                "expires_at": stored.payload.expires_at + timedelta(days=365),
            },
        )
        # Sanity: the edit really is invisible to the claims projection, so a
        # pass here is evidence about the digest check and nothing else.
        assert stable_claims(drifted) == stable_claims(stored.payload)
        async with sqlite_db.transaction(write_lock=True) as connection:
            await connection.execute(
                "UPDATE run_attestations SET payload_json = ?",
                (drifted.model_dump_json(),),
            )

        replayed = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )

    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "attestation_conflict"
    # The edited row's envelope is not reported back: no digest, no expiry.
    assert first.json()["digest"] not in replayed.text
    assert str(first.json()["expires_at"]) not in replayed.text


async def test_a_stored_row_naming_another_deployment_discloses_nothing(
    sqlite_db,
) -> None:
    """The scoped read is scoped on the signed payload, not just the flat column.

    ``find_for_deployment`` narrows by the ``deployment_ref`` *column*, written
    at insert time and never checked against the payload that was signed. For a
    row this repository wrote the two agree; for an inconsistent one they need
    not, and the caller's next move is to report the row's digest and expiry
    back over the wire -- which is precisely the cross-deployment disclosure
    that scoped read exists to prevent (round 3's G6).

    So: a row whose column says it is ours and whose signed payload names
    another deployment must read as absent, and the caller must get the fixed
    opaque conflict with no digest and no expiry -- the same answer it would get
    for a sibling deployment's correlation.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        body = _attestation_body(deployment.deployment_ref, fingerprint="sha256:ours")
        first = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        assert first.status_code == 201

        stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
            TENANT_A,
            "run-correlation",
        )
        assert stored is not None
        # Signed payload now names a different deployment; the flat column,
        # which is what the SELECT filters on, still names ours.
        elsewhere = stored.payload.model_copy(update={"deployment_ref": "somewhere-else"})
        async with sqlite_db.transaction(write_lock=True) as connection:
            await connection.execute(
                "UPDATE run_attestations SET payload_json = ?",
                (elsewhere.model_dump_json(),),
            )

        probe = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment.deployment_ref, fingerprint="sha256:probe"),
            headers=api_key_headers(SECRET_A),
        )

    assert probe.status_code == 409
    assert probe.json()["detail"]["code"] == "attestation_conflict"
    # Nothing of the row leaks: not its digest, not its expiry.
    assert first.json()["digest"] not in probe.text
    assert str(first.json()["expires_at"]) not in probe.text


async def test_a_stored_row_naming_another_run_discloses_nothing(
    sqlite_db,
) -> None:
    """The scoped read checks all three signed identities, not just two.

    ``find_for_deployment`` re-derives the tenant and the deployment from the
    signed payload, but for one revision it trusted the ``correlation_id``
    *column* -- leaving the defect it had just fixed alive one field over. A row
    whose column says run A while its signed payload says run B read as present,
    and the route's next move is to report that row's digest and expiry in run
    A's conflict response.

    Narrower than the cross-deployment leak (same tenant, same deployment, and
    it needs write access to the table) but the identical shape: evidence about
    one run disclosed under another run's name. Such a row must read as absent,
    and the caller must get the fixed opaque conflict with no digest and no
    expiry.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        body = _attestation_body(deployment.deployment_ref, fingerprint="sha256:ours")
        first = client.post(
            "/v1/enforcement/attestations",
            json=body,
            headers=api_key_headers(SECRET_A),
        )
        assert first.status_code == 201

        stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
            TENANT_A,
            "run-correlation",
        )
        assert stored is not None
        # Signed payload now names a different run; the flat column, which is
        # what the SELECT filters on, still names the one we will ask about.
        other_run = stored.payload.model_copy(update={"correlation_id": "run-elsewhere"})
        async with sqlite_db.transaction(write_lock=True) as connection:
            await connection.execute(
                "UPDATE run_attestations SET payload_json = ?",
                (other_run.model_dump_json(),),
            )

        probe = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment.deployment_ref, fingerprint="sha256:probe"),
            headers=api_key_headers(SECRET_A),
        )

    assert probe.status_code == 409
    assert probe.json()["detail"]["code"] == "attestation_conflict"
    # Nothing of the other run's attestation leaks: not its digest, not its expiry.
    assert first.json()["digest"] not in probe.text
    assert str(first.json()["expires_at"]) not in probe.text


async def _two_deployment_app(sqlite_db):
    """Serve one tenant that owns two deployments.

    Scope is tenant-and-workspace wide (``require_deployment_scope``), so one
    principal may address both -- which is what makes the cross-deployment
    read-back below reachable rather than hypothetical.
    """
    _, deployment_a = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-enforcement-solo"),
        deployment_ref="enforcement-solo",
        tenant_id=TENANT_A,
        auth_config=_auth_config(),
    )
    _, deployment_b = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-enforcement-second"),
        deployment_ref="enforcement-second",
        tenant_id=TENANT_A,
        auth_config=_auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment_a.deployment_ref,
        tenant_id=TENANT_A,
        auth_config=_auth_config(),
    )
    return app, deployment_a, deployment_b


async def test_a_losing_attestation_from_another_deployment_learns_nothing_about_it(
    sqlite_db,
) -> None:
    """A 409 must not become a read of another deployment's evidence.

    ``run_attestations`` is unique on ``(tenant_id, correlation_id)``, so a
    correlation already attested by deployment B makes deployment A's
    submission lose. The audited defect is what happened next: the loser's
    body was filled from the *unscoped* ``find_by_correlation``, so a caller
    naming its own deployment could probe correlations belonging to any
    sibling deployment in the tenant and read back B's authoritative digest
    and expiry -- an oracle over another deployment's runs.

    The conflict is still reported. What it no longer carries is B's evidence.
    """
    app, deployment_a, deployment_b = await _two_deployment_app(sqlite_db)

    with TestClient(app) as client:
        owner = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_b.deployment_ref,
                fingerprint="sha256:beta-secret",
            ),
            headers=api_key_headers(SECRET_A),
        )
        probe = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_a.deployment_ref,
                fingerprint="sha256:probe",
            ),
            headers=api_key_headers(SECRET_A),
        )

    assert owner.status_code == 201
    assert probe.status_code == 409
    assert "beta-secret" not in probe.text
    assert owner.json()["digest"] not in probe.text
    assert str(owner.json()["expires_at"]) not in probe.text


async def test_a_row_rewritten_onto_this_deployment_discloses_nothing(
    sqlite_db,
) -> None:
    """Passing the scoped read is not the same as being this deployment's row.

    The scoped read re-derives all three identities from the signed payload, so
    the two tests above -- where only one of them was rewritten -- now read as
    absent. This is the case they leave open: rewrite the flat
    ``deployment_ref`` column *and* the signed payload's ``deployment_ref``
    together, so every identity the lookup checks agrees and the row is
    returned. The envelope columns are left exactly as deployment B's insert
    wrote them, so the digest still on the row is B's.

    ``is_identical_resubmission`` correctly refuses -- the digest no longer
    derives from the rewritten payload -- but refusing only decided
    ``authoritative``. The 409 body was still filled from the row, so the
    caller read back B's digest and expiry: the cross-deployment oracle of
    round 3's G6, reached through a rewritten row instead of an unscoped read.

    Classification and disclosure are separate decisions, and this pins the
    second one. A row whose digest does not re-derive from the payload beside
    it is not evidence anyone may be told about, so the answer is the fixed
    opaque conflict.
    """
    app, deployment_a, deployment_b = await _two_deployment_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        owner = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_b.deployment_ref,
                fingerprint="sha256:beta-secret",
            ),
            headers=api_key_headers(SECRET_A),
        )
        assert owner.status_code == 201

        stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
            TENANT_A,
            "run-correlation",
        )
        assert stored is not None
        # Both identities moved onto A; digest, signature, key id and algorithm
        # stay as B's insert wrote them, so the row still reports B's evidence.
        rewritten = stored.payload.model_copy(
            update={"deployment_ref": deployment_a.deployment_ref},
        )
        async with sqlite_db.transaction(write_lock=True) as connection:
            await connection.execute(
                "UPDATE run_attestations SET payload_json = ?, deployment_ref = ?",
                (rewritten.model_dump_json(), deployment_a.deployment_ref),
            )

        probe = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_a.deployment_ref,
                fingerprint="sha256:probe",
            ),
            headers=api_key_headers(SECRET_A),
        )

    assert probe.status_code == 409
    assert probe.json()["detail"]["code"] == "attestation_conflict"
    # Nothing of B's attestation leaks: not its digest, not its expiry.
    assert owner.json()["digest"] not in probe.text
    assert str(owner.json()["expires_at"]) not in probe.text


async def test_a_row_rewritten_with_a_recomputed_digest_discloses_nothing(
    sqlite_db,
) -> None:
    """Re-deriving the digest is not proof the row is this caller's to read.

    ``test_a_row_rewritten_onto_this_deployment_discloses_nothing`` above left
    the envelope alone, so the digest no longer derived from the rewritten
    payload and re-derivation caught it. This is the case that leaves open:
    rewrite the payload's ``deployment_ref``, the flat column, *and* the
    ``digest`` column together, so the payload does hash to the digest beside
    it and the consistency check answers yes.

    No attacker is needed to produce this shape. A migration that re-homes
    deployments and recomputes digests, but cannot re-sign because it does not
    hold the key, writes exactly this row.

    What such a rewrite cannot reproduce is the signature: it covers the digest
    the row had when B signed it, and the recomputed digest is a different one.
    Every claim other than ``deployment_ref`` is still B's -- including the
    issuance window -- so a 409 filled from this row hands A the expiry of an
    attestation it does not own. The answer is the fixed opaque conflict.
    """
    app, deployment_a, deployment_b = await _two_deployment_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        owner = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_b.deployment_ref,
                fingerprint="sha256:beta-secret",
            ),
            headers=api_key_headers(SECRET_A),
        )
        assert owner.status_code == 201

        stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
            TENANT_A,
            "run-correlation",
        )
        assert stored is not None
        # Identity moves onto A and the digest is recomputed over the rewritten
        # payload, so the row is internally consistent. The signature triple is
        # left as B's insert wrote it: it still covers B's original digest.
        rewritten = stored.payload.model_copy(
            update={"deployment_ref": deployment_a.deployment_ref},
        )
        recomputed = attestation_digest(rewritten)
        assert recomputed != stored.digest
        async with sqlite_db.transaction(write_lock=True) as connection:
            await connection.execute(
                "UPDATE run_attestations SET payload_json = ?, deployment_ref = ?, digest = ?",
                (rewritten.model_dump_json(), deployment_a.deployment_ref, recomputed),
            )

        probe = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_a.deployment_ref,
                fingerprint="sha256:probe",
            ),
            headers=api_key_headers(SECRET_A),
        )

    assert probe.status_code == 409
    assert probe.json()["detail"]["code"] == "attestation_conflict"
    # B's expiry is the claim the rewrite preserves, so it is the one that
    # leaked before this gate: only ``deployment_ref`` moved.
    assert str(owner.json()["expires_at"]) not in probe.text
    assert owner.json()["digest"] not in probe.text


async def test_a_row_stripped_of_its_signature_discloses_nothing(
    sqlite_db,
) -> None:
    """A row cannot demote itself into the weaker disclosure check.

    The ``UPDATE`` that rewrites ``payload_json`` and ``digest`` can null the
    signature triple in the same statement. If the gate decided "is this row
    signed?" by reading the row's own columns, that would be enough to select
    the unsigned branch -- which asks only for internal consistency, and the
    rewrite already satisfies it. The leak would move, not close.

    So the branch is chosen by the deployment's signing posture instead. This
    deployment holds a verifier, so every row must verify, and a stripped row
    verifies as nothing.
    """
    app, deployment_a, deployment_b = await _two_deployment_app(sqlite_db)
    _wire_signer(app, "attest-k1", b"enforcement-attestation-key")

    with TestClient(app) as client:
        owner = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_b.deployment_ref,
                fingerprint="sha256:beta-secret",
            ),
            headers=api_key_headers(SECRET_A),
        )
        assert owner.status_code == 201

        stored = await RunAttestationRepository(sqlite_db).find_by_correlation(
            TENANT_A,
            "run-correlation",
        )
        assert stored is not None
        rewritten = stored.payload.model_copy(
            update={"deployment_ref": deployment_a.deployment_ref},
        )
        async with sqlite_db.transaction(write_lock=True) as connection:
            await connection.execute(
                "UPDATE run_attestations SET payload_json = ?, deployment_ref = ?, "
                "digest = ?, signature = NULL, signing_key_id = NULL, "
                "signing_algorithm = NULL",
                (
                    rewritten.model_dump_json(),
                    deployment_a.deployment_ref,
                    attestation_digest(rewritten),
                ),
            )

        probe = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment_a.deployment_ref,
                fingerprint="sha256:probe",
            ),
            headers=api_key_headers(SECRET_A),
        )

    assert probe.status_code == 409
    assert probe.json()["detail"]["code"] == "attestation_conflict"
    assert str(owner.json()["expires_at"]) not in probe.text
    assert owner.json()["digest"] not in probe.text


async def test_a_losing_attestation_still_learns_its_own_deployments_evidence(
    sqlite_db,
) -> None:
    """The positive control: same-deployment conflicts keep reporting the winner.

    Closing the cross-deployment read must not degrade the ordinary case into
    an opaque error -- a second attestation for a run *this* deployment owns is
    entitled to learn which attestation is actually in force, and
    ``test_an_attestation_that_loses_to_an_earlier_one_is_reported_as_not_authoritative``
    above is the behaviour that must survive.
    """
    app, deployment_a, _ = await _two_deployment_app(sqlite_db)

    with TestClient(app) as client:
        first = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment_a.deployment_ref, fingerprint="sha256:first"),
            headers=api_key_headers(SECRET_A),
        )
        second = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment_a.deployment_ref, fingerprint="sha256:second"),
            headers=api_key_headers(SECRET_A),
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["authoritative"] is False
    assert second.json()["digest"] == first.json()["digest"]


async def test_a_heartbeat_is_recorded_and_reported_below_enforced(sqlite_db) -> None:
    """A heartbeat proves no inventory, so its ``enforced`` claim cannot survive."""
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        posted = client.post(
            "/v1/enforcement/heartbeats",
            json={
                "deployment_ref": deployment.deployment_ref,
                "graph_version": deployment.graph_version_ref,
                "adapter_version": "1.0",
                "reported_level": "enforced",
            },
            headers=api_key_headers(SECRET_A),
        )
        status_response = client.get(
            f"/v1/enforcement/deployments/{deployment.deployment_ref}/status",
            headers=api_key_headers(SECRET_A),
        )

    assert posted.status_code == 201
    assert posted.json()["heartbeat_id"]
    assert status_response.status_code == 200
    assert status_response.json()["governance_level"] == "observed"


async def test_a_run_status_read_is_scoped_to_the_callers_tenant(sqlite_db) -> None:
    """Another tenant's attestation under the same correlation reads as absent."""
    app, deployment = await _single_tenant_app(sqlite_db)
    issued_at = datetime.now(UTC)
    await RunAttestationRepository(sqlite_db).record(
        sign_attestation(
            RunAttestationPayload(
                correlation_id="foreign-run",
                tenant_id=TENANT_B,
                deployment_ref=deployment.deployment_ref,
                graph_version=deployment.graph_version_ref,
                adapter_version="1.0",
                inventory_fingerprint="sha256:inventory",
                inventory_coverage="complete",
                tool_count=1,
                claimed_level="enforced",
                issued_at=issued_at,
                expires_at=issued_at + timedelta(minutes=15),
            ),
            None,
        )
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/enforcement/deployments/{deployment.deployment_ref}/runs/foreign-run",
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 200
    assert response.json()["governance_level"] == "admission"


async def test_the_compat_alias_serves_the_same_decision_surface(sqlite_db) -> None:
    """The unversioned alias is registered alongside /v1, as every other route is."""
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(
            "/enforcement/decisions",
            json=_decision_body(deployment.deployment_ref, key="compat-key"),
            headers=api_key_headers(SECRET_A),
        )

    assert response.status_code == 200
    assert response.json()["kind"] == "deny"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/enforcement/decisions", None),
        ("/v1/enforcement/registrations", None),
    ],
)
async def test_an_unauthenticated_request_reaches_no_route(sqlite_db, path, payload) -> None:
    """The authentication middleware answers before any handler is entered."""
    app, _ = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        response = client.post(path, json=payload or {})

    assert response.status_code == 401
    assert await _decision_row_count(sqlite_db) == 0
