"""HMAC-SHA256 signing utility for webhook payloads.

Used to sign outgoing webhook payloads so receivers can verify authenticity.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_payload(payload_bytes: bytes, secret: str) -> str:
    """Sign a payload with HMAC-SHA256 and return the hex digest.

    Args:
        payload_bytes: The raw bytes of the webhook payload to sign.
        secret: The shared secret string for the subscription.

    Returns:
        A lowercase hex string of the HMAC-SHA256 signature.
    """
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, secret: str, signature_header: str | None) -> bool:
    """Verify a ``sha256=<hex>`` signature header against a raw payload.

    The inbound counterpart of :func:`sign_payload`, shaped for GitHub's
    ``X-Hub-Signature-256`` convention: the header value is the hex HMAC-SHA256
    digest prefixed with ``sha256=``. Comparison uses
    :func:`hmac.compare_digest` so it stays constant-time; an absent or
    malformed header verifies as ``False`` rather than raising.

    Args:
        payload: The raw request body bytes exactly as received.
        secret: The shared webhook secret.
        signature_header: The ``X-Hub-Signature-256`` header value, or ``None``.

    Returns:
        True only when the header carries the correct digest for ``payload``.
    """
    if not signature_header:
        return False
    scheme, separator, digest = signature_header.partition("=")
    if not separator or scheme.lower() != "sha256" or not digest:
        return False
    return hmac.compare_digest(sign_payload(payload, secret), digest.strip().lower())
