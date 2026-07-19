"""Canonical byte encoding for the material a provenance signature covers.

A signature is taken over :func:`signable_bytes`, NOT over the bare digest.
Binding ``key_id`` and ``algorithm`` *inside* the signed bytes closes a
downgrade/substitution attack: an attacker who can rewrite the stored
``signing_key_id`` (or ``algorithm``) to point at a weaker or attacker-held key
cannot make the old signature re-verify, because those two fields are part of
what was signed. The encoding reuses ``to_json_value`` (sorted keys, no
whitespace) so the bytes are deterministic and identical on the sign and verify
sides regardless of insertion order.
"""

from __future__ import annotations

from zeroth.platform.storage.json import to_json_value


def signable_bytes(digest: str, key_id: str, algorithm: str) -> bytes:
    """Return the canonical bytes a provenance signature is computed over.

    ``key_id`` and ``algorithm`` are folded into the signed material so they
    are cryptographically bound to the digest and cannot be swapped after the
    fact without invalidating the signature.
    """
    return to_json_value({"digest": digest, "key_id": key_id, "alg": algorithm}).encode("utf-8")
