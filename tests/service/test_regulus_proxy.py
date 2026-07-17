"""Security invariants for the admin-gated Regulus console proxy.

The proxy is the single controlled path into the bundled econ control plane
(see ``regulus_proxy_api`` / REGULUS-FINDINGS.md). Two properties must hold:

* only platform admins can reach it (``ECON_ADMIN`` is granted to ADMIN only);
* the GET forwarder allowlists read paths and rejects traversal / auth routes.
"""

from zeroth.core.identity import ServiceRole
from zeroth.core.service.authorization import ROLE_PERMISSIONS, Permission
from zeroth.core.service.regulus_proxy_api import _get_allowed


def test_econ_admin_is_admin_only():
    # ADMIN holds every permission; OPERATOR and REVIEWER must NOT hold ECON_ADMIN
    # (else a non-admin console principal could read global econ data).
    assert Permission.ECON_ADMIN in ROLE_PERMISSIONS[ServiceRole.ADMIN]
    assert Permission.ECON_ADMIN not in ROLE_PERMISSIONS[ServiceRole.OPERATOR]
    assert Permission.ECON_ADMIN not in ROLE_PERMISSIONS[ServiceRole.REVIEWER]


def test_get_allowlist_accepts_known_read_paths():
    for path in (
        "dashboard/kpis",
        "registry/capabilities",
        "enforcement/actions",
        "reconciliation/calibration-summary",
        "budget/status",
    ):
        assert _get_allowed(path), path


def test_get_allowlist_rejects_auth_traversal_and_unknown():
    for path in (
        "auth/token",  # the blocked issuer
        "auth/me",  # login surface — not exposed
        "dashboard/../auth/token",  # path traversal
        "/etc/passwd",  # absolute
        "registry/../../secret",  # traversal past the allowed prefix
        "something-else",  # not on the allowlist
    ):
        assert not _get_allowed(path), path
