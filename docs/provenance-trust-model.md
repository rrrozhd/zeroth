# Provenance Trust Model (WS-D)

Zeroth records two kinds of provenance:

- **Deployment attestations** — a stable snapshot digest per deployment version
  (`deployment_versions.attestation_digest`), signed with a keyed signature.
- **Audit chain** — each `NodeAuditRecord` hashes its own content and pins its
  predecessor's digest, and each record's digest is signed.

Both layers separate two independent guarantees. Read them as a stack, not a
single "verified" bit.

## The two axes

| Axis | Mechanism | Catches | Does **not** catch |
|------|-----------|---------|--------------------|
| **Digest** (tamper-**evident**) | unkeyed SHA-256 recomputed from the live snapshot / chained across records | accidental corruption, a naive edit, a broken predecessor link | a malicious writer who edits the data **and recomputes the digest** |
| **Signature** (tamper-**resistant**) | keyed signature over the digest | forgery by anyone without the signing key | anything the key holder chooses to sign (see custody, below) |

Neither masks the other. A verification result reports both:

- `verified` / `digest_verified` — the digest axis.
- `signature_verified` — the signature axis, **three-state**:
  - `true` — every signed record/attestation in scope verified under a trusted key;
  - `false` — a signature is present and failed (tampered or wrong key);
  - `null` — **unsigned-legacy**: nothing in scope was signed. Render this
    **neutral**, never green and never red. It asserts only that the *digest*
    chain is intact.

## What a valid signature asserts, per mode

Configured via `ZEROTH_PROVENANCE__MODE` (`env` | `kms` | `off`). The signing key
resolves through the **shared WS-F `SecretProvider`** (logical name
`signing.deployment`), never a second env reader.

> **Note on `algorithm`.** `ZEROTH_PROVENANCE__ALGORITHM` is *advisory* — the
> effective algorithm is derived from `mode` (`env` → HS256, `kms` → Ed25519).
> Setting it inconsistently with `mode` does not change the algorithm actually
> used; it exists for documentation/telemetry only.

### `mode="env"` — HMAC-SHA256 (default, dev)

- **Key location:** the shared secret provider resolves `signing.deployment`
  (env: `SIGNING_DEPLOYMENT`, or the tenant-scoped
  `ZEROTH_SECRET__{TENANT}__SIGNING_DEPLOYMENT`). With the Vault backend the same
  logical name resolves from the KV mount.
- **What a valid signature asserts:** *"a holder of the shared HMAC key produced
  this."* It is **key-custody-bounded**.
- **What it is NOT:** this is a symmetric secret. Anyone who can read the key can
  forge an identical signature. **It is NOT PKI and NOT non-repudiation** — it
  cannot prove *which* party signed, only that *someone with the key* did. Do not
  present env-HMAC provenance as cryptographic non-repudiation to a third party.
- **No key configured:** the process runs **unsigned-legacy** after a startup
  warning. Records verify as `signature_verified = null` (neutral). This keeps
  dev boxes and the existing test suite working without minting rows that read as
  signed. It is intentionally *not silent* — the warning is the tell.

### `mode="kms"` — Ed25519 (the strong claim)

- **Key location:** the private key resolves through the secret provider (e.g. a
  Vault mount); the public verify keys are carried in
  `ZEROTH_PROVENANCE__PUBLIC_KEYS_JSON` as `{key_id: public-key-hex}`.
- **What a valid signature asserts:** *"the holder of the private key for
  `key_id` signed this digest,"* and — because verification needs only the
  **public** key — a verifier who never holds the private key **cannot forge**.
  This is the path that backs a "signed & verifiable" claim to an external party.
- **Non-repudiation** in the full sense requires the private key to live in an
  **external KMS/HSM that signs without exposing the key material**. The
  `Ed25519Signer` interface supports that split (public keys for verify, private
  key held elsewhere); wiring a specific managed-KMS backend that signs remotely
  is deferred — a locally-held Ed25519 private key is asymmetric-forgery-resistant
  but its custody story is only as strong as the store it resolves from.

### `mode="off"` — signing disabled

- Uses a `NullSigner`: it produces no signature (records stay unsigned-legacy)
  and `verify` always returns `false`, so a misconfiguration can never mint a row
  that reads as verified. Records verify as `signature_verified = null` (neutral).

## Fail-closed posture

- The signature is taken over `signable_bytes(digest, key_id, algorithm)` — the
  **key id and algorithm are inside the signed bytes**. An attacker who rewrites
  the stored `signing_key_id` to point at a weaker or attacker-held key cannot
  make the old signature re-verify (downgrade/substitution is closed).
- The digest is **byte-identical whether or not a record is signed** (signature
  fields are excluded from the digest input). Adding a signature never
  retroactively breaks a pre-signing record's stored digest.
- Missing key material on the **strong** path (`mode="kms"`) fails **closed at
  startup** (raises), mirroring the Vault secret backend. The dev path
  (`mode="env"` with no key) degrades to unsigned-legacy + warning rather than
  crashing.

## Key rotation

Signers verify by the `key_id` a signature claims, so a retired key can stay
**verify-only** after rotation: point the active `signing_key_id` at the new key
and keep the old one available to verify, and previously-signed records continue
to verify while new records are signed under the new key.

Signing and verification are built as **separate providers**, because they have
different lifetimes. The signer answers "what do we sign with now", and rotation
moves that answer forward. The verifier answers "was this signed by a key we
recognise", which stays true for keys that stopped signing long ago. Building one
provider for both would make a rotation retroactively unverify every record
written before it.

Name the retired keys per mode:

- `mode="kms"` — `public_keys_json` already carries every acceptable verify key;
  add the retired `key_id` there and it keeps verifying.
- `mode="env"` — `retired_keys_json`, a `{key_id: key-material}` object. This is
  verify-only: signing always uses `signing_key_id`, so a key listed here can
  never mint a new record.

The verifier is built even when signing is off (`mode="off"`, or `env` with no
resolvable key). A deployment that stops signing still holds records it signed
earlier, and those do not become unverifiable because new ones are unsigned.

Retention is not cosmetic. Verification is what the run-attestation `409` uses to
decide whether a stored row's digest and expiry may be disclosed at all, so a
rotation performed without naming the retired key makes legitimate conflicts
answer opaquely — fail-closed, but it costs a caller evidence it was entitled to.

## Rounding out the honest claim

- "Deployment provenance" and "tamper-evident audit" are true at the **digest**
  layer today for every deployment.
- "Signed & verifiable" provenance is true under `mode="env"` **bounded by key
  custody** (not non-repudiation), and reaches third-party-verifiable /
  non-repudiation strength only on the `mode="kms"` Ed25519 + external-KMS path.
- The console badge and the API responses reflect exactly this: unsigned-legacy
  is shown neutral, a present-and-valid signature green, a present-and-invalid
  signature red.
