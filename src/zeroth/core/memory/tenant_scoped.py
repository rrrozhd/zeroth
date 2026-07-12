"""Tenant-scoping wrapper for memory connectors (defense-in-depth isolation).

Single-tenant-per-deployment is the product model, but deployments may share a
physical backend (Redis / Postgres / Chroma / Elastic). Without this wrapper the
``SHARED`` memory scope resolves — inside governai's ``ScopedMemoryConnector`` —
to the constant literal ``"__shared__"`` with **no tenant**, so two tenant
services pointed at one backend would read each other's shared memory.

``TenantScopedMemoryConnector`` sits **below** ``ScopedMemoryConnector`` in the
wrapper stack::

    ScopedMemoryConnector(TenantScopedMemoryConnector(Auditing(raw)))

Scoped resolves ``scope -> target`` first (producing ``"__shared__"`` / run_id /
thread_id), then TenantScoped rewrites that already-resolved ``target`` to a
tenant-namespaced form before the auditing/raw layers ever see it. Every
zeroth connector keys its storage on ``(scope, target, key)``, so rewriting the
target namespaces all backends uniformly — SHARED included.

The wrapper is **fail-closed**: an empty or missing ``tenant_id`` raises
``TenantScopeError`` rather than silently coercing to a shared namespace. An
explicit ``"default"`` is permitted as the reserved single-tenant sentinel.
"""

from __future__ import annotations

import hashlib
import re

from governai.memory.models import MemoryEntry, MemoryScope
from governai.models.common import JSONValue

# Any run of characters outside ``[a-z0-9]`` is replaced by a single dash when
# building the human-readable slug prefix. This mirrors the sanitization that
# backend name-normalizers (notably Chroma's collection-name rule) apply, so
# the readable prefix survives them unchanged.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Separator between the tenant slug and the resolved target. Two underscores so
# it is visually distinct from single-underscore separators inside targets.
_TENANT_SEP = "__"


class TenantScopeError(RuntimeError):
    """Raised when a memory operation is attempted without a valid tenant.

    Fail-closed guard: an empty/missing tenant is never coerced into a shared
    namespace. An explicit ``"default"`` sentinel is allowed.
    """


def tenant_slug(tenant_id: str | None) -> str:
    """Return a collision-free, backend-safe slug for *tenant_id*.

    The slug is ``f"{sanitized}-{hash8}"`` where ``sanitized`` is a readable
    ``[a-z0-9-]`` projection of the tenant id and ``hash8`` is the first eight
    hex characters of ``sha256(raw_tenant_id)``.

    The hash is the load-bearing part: it is pure hex, so it survives any
    backend name-normalizer that collapses non-alphanumerics (e.g. Chroma maps
    ``[^a-zA-Z0-9]+ -> "_"``). Because the hash is computed over the **raw**
    id, two ids that sanitize to the same prefix — ``"tenant-a"`` and
    ``"tenant_a"`` both project to ``"tenant-a"`` — still yield distinct slugs,
    so they can never merge into one backend namespace.

    Raises ``TenantScopeError`` when *tenant_id* is empty or missing.
    """
    if tenant_id is None:
        raise TenantScopeError("memory access requires a tenant_id (got None)")
    raw = tenant_id.strip()
    if not raw:
        raise TenantScopeError("memory access requires a non-empty tenant_id")
    sanitized = _SLUG_STRIP_RE.sub("-", raw.lower()).strip("-")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if not sanitized:
        # Tenant id was entirely non-alphanumeric; the hash alone is unique.
        return digest
    return f"{sanitized}-{digest}"


class TenantScopedMemoryConnector:
    """Rewrites a resolved memory ``target`` into a tenant-namespaced target.

    Implements the governai ``MemoryConnector`` protocol
    (``read``/``write``/``delete``/``search`` with a keyword-only ``target``).
    Instances are created per-resolve (like ``ScopedMemoryConnector``), so the
    tenant is bound at construction here — never on the shared resolver/raw
    connector singletons.
    """

    def __init__(self, inner: object, *, tenant_id: str | None) -> None:
        # ``tenant_slug`` fails closed on an empty/missing tenant, so an
        # unscoped connector can never be constructed.
        self._inner = inner
        self._tenant_slug = tenant_slug(tenant_id)

    def _rewrite(self, target: str | None) -> str:
        """Namespace an already-resolved target under this tenant.

        Applies to ALL scopes: RUN (run_id), THREAD (thread_id) and SHARED
        (``"__shared__"``) targets are each prefixed, so no scope can be read
        across tenants on a shared backend.
        """
        return f"{self._tenant_slug}{_TENANT_SEP}{target or ''}"

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        return await self._inner.read(key, scope, target=self._rewrite(target))

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        await self._inner.write(key, value, scope, target=self._rewrite(target))

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        await self._inner.delete(key, scope, target=self._rewrite(target))

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        return await self._inner.search(query, scope, target=self._rewrite(target))
