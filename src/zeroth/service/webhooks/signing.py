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
