"""Secret provider and resolution helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from zeroth.core.secrets.redaction import SecretRedactor

if TYPE_CHECKING:
    # Typed against execution_units.EnvironmentVariable without importing it at
    # runtime — execution_units.runner imports SecretResolver from here, so a
    # concrete import would create an import-time cycle.
    from zeroth.core.execution_units.models import EnvironmentVariable


class SecretResolutionError(RuntimeError):
    """A required secret could not be resolved and no fallback is permitted.

    Raised on a fail-closed path (e.g. ``allow_env_fallback=False``) so a
    missing key surfaces loudly at call time instead of silently falling
    through to a process-global environment variable. Carries only the logical
    name — NEVER a concrete secret value.
    """


def normalize_secret_name(name: str) -> str:
    """Map a logical secret name to its ENV-VAR token form.

    ``'llm.openai'`` -> ``'LLM_OPENAI'``; ``'signing.deployment'`` ->
    ``'SIGNING_DEPLOYMENT'``. Dots and dashes become underscores and the whole
    string is upper-cased so it composes into the
    ``ZEROTH_SECRET__{TENANT}__{NAME}`` convention.
    """
    return name.replace(".", "_").replace("-", "_").upper()


class SecretProvider(Protocol):
    """Interface for resolving secret references to concrete values.

    The optional ``tenant_id`` keyword scopes resolution to a single tenant
    (one value per deployment under the single-tenant model). It defaults to
    ``None`` so existing ref-based callers (http client, execution units) keep
    working unchanged.
    """

    def resolve(
        self, secret_ref: str, *, tenant_id: str | None = None
    ) -> str | None:  # pragma: no cover - protocol
        """Resolve a single secret reference."""

    def resolve_many(
        self, refs: list[str], *, tenant_id: str | None = None
    ) -> dict[str, str]:  # pragma: no cover - protocol
        """Resolve multiple secret references at once."""

    def resolve_secret(
        self,
        logical_name: str,
        *,
        tenant_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> str | None:  # pragma: no cover - protocol
        """Resolve a *logical* secret name (e.g. ``'llm.openai'``) to a value.

        Unlike :meth:`resolve`, which takes a concrete reference, this maps a
        stable logical key onto whatever concrete secret the backend holds for
        the given tenant. Returns ``None`` when unresolved; the caller decides
        whether to fail closed.
        """


class EnvSecretProvider:
    """Resolve secret references by reading process or injected environment variables.

    Tenant-scoped lookups follow the convention
    ``ZEROTH_SECRET__{TENANT}__{NAME}`` and fall back to the bare name, so a
    single-tenant deployment can key secrets off ``deployment.tenant_id`` while
    dev setups keep using plain env vars.
    """

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = dict(environment or {})

    def _scoped_name(self, tenant_id: str, name_token: str) -> str:
        """Compose the ``ZEROTH_SECRET__{TENANT}__{NAME}`` environment-variable key."""
        return f"ZEROTH_SECRET__{normalize_secret_name(tenant_id)}__{name_token}"

    def resolve(self, secret_ref: str, *, tenant_id: str | None = None) -> str | None:
        """Resolve a concrete ref from the environment; the tenant-scoped key wins."""
        # Tenant-scoped override wins when present; the bare ref is the
        # back-compatible fallback (http client passes ``config.secret_key``).
        if tenant_id:
            scoped = self._scoped_name(tenant_id, normalize_secret_name(secret_ref))
            if scoped in self._environment:
                return self._environment[scoped]
        return self._environment.get(secret_ref)

    def resolve_many(self, refs: list[str], *, tenant_id: str | None = None) -> dict[str, str]:
        """Resolve several refs; refs that do not resolve are omitted from the result."""
        return {
            ref: value
            for ref in refs
            if (value := self.resolve(ref, tenant_id=tenant_id)) is not None
        }

    def resolve_secret(
        self,
        logical_name: str,
        *,
        tenant_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> str | None:
        """Resolve a logical name; the tenant-scoped token wins over the bare env token."""
        name_token = normalize_secret_name(logical_name)
        if tenant_id:
            scoped = self._scoped_name(tenant_id, name_token)
            if scoped in self._environment:
                return self._environment[scoped]
        # Bare fallback: the un-scoped logical env token (e.g. ``LLM_OPENAI``).
        return self._environment.get(name_token)

    # Native async variants: lookups are in-memory dict reads, so they resolve
    # immediately without a thread hop.
    async def resolve_async(self, secret_ref: str, *, tenant_id: str | None = None) -> str | None:
        """Async variant of :meth:`resolve` (pure dict read; no thread hop needed)."""
        return self.resolve(secret_ref, tenant_id=tenant_id)

    async def resolve_many_async(
        self, refs: list[str], *, tenant_id: str | None = None
    ) -> dict[str, str]:
        """Async variant of :meth:`resolve_many`."""
        return self.resolve_many(refs, tenant_id=tenant_id)

    async def resolve_secret_async(
        self,
        logical_name: str,
        *,
        tenant_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> str | None:
        """Async variant of :meth:`resolve_secret`."""
        return self.resolve_secret(logical_name, tenant_id=tenant_id, deployment_ref=deployment_ref)


async def resolve_async(
    provider: SecretProvider, secret_ref: str, *, tenant_id: str | None = None
) -> str | None:
    """Resolve one secret ref without blocking the event loop.

    Awaits the provider's native ``resolve_async`` when it has one; sync-only
    providers (which may perform blocking I/O, e.g. Vault over HTTP) run via
    ``asyncio.to_thread``.
    """
    native = getattr(provider, "resolve_async", None)
    if native is not None:
        return await native(secret_ref, tenant_id=tenant_id)
    if tenant_id is None:
        # Bare call keeps duck-typed providers without the kwarg working (the
        # sync call sites never passed tenant_id either).
        return await asyncio.to_thread(provider.resolve, secret_ref)
    return await asyncio.to_thread(provider.resolve, secret_ref, tenant_id=tenant_id)


async def resolve_many_async(
    provider: SecretProvider, refs: list[str], *, tenant_id: str | None = None
) -> dict[str, str]:
    """Resolve several secret refs without blocking the event loop."""
    native = getattr(provider, "resolve_many_async", None)
    if native is not None:
        return await native(refs, tenant_id=tenant_id)
    if tenant_id is None:
        return await asyncio.to_thread(provider.resolve_many, refs)
    return await asyncio.to_thread(provider.resolve_many, refs, tenant_id=tenant_id)


async def resolve_secret_async(
    provider: SecretProvider,
    logical_name: str,
    *,
    tenant_id: str | None = None,
    deployment_ref: str | None = None,
) -> str | None:
    """Resolve a logical secret name without blocking the event loop."""
    native = getattr(provider, "resolve_secret_async", None)
    if native is not None:
        return await native(logical_name, tenant_id=tenant_id, deployment_ref=deployment_ref)
    return await asyncio.to_thread(
        lambda: provider.resolve_secret(
            logical_name, tenant_id=tenant_id, deployment_ref=deployment_ref
        )
    )


class SecretResolver:
    """Resolve environment-variable models into a concrete runtime environment."""

    def __init__(self, provider: SecretProvider) -> None:
        self.provider = provider
        self._resolved: dict[str, str] = {}

    def resolve_environment_variables(
        self,
        variables: list[EnvironmentVariable],
    ) -> dict[str, str]:
        """Resolve variable models into an env dict; a missing secret ref raises ``KeyError``."""
        refs = [item.secret_ref for item in variables if item.secret_ref]
        loaded = self.provider.resolve_many([ref for ref in refs if ref is not None])
        return self._merge_resolved(variables, loaded)

    async def resolve_environment_variables_async(
        self,
        variables: list[EnvironmentVariable],
    ) -> dict[str, str]:
        """Async variant for event-loop callers (execution-unit dispatch)."""
        refs = [item.secret_ref for item in variables if item.secret_ref]
        loaded = await resolve_many_async(self.provider, [ref for ref in refs if ref is not None])
        return self._merge_resolved(variables, loaded)

    def _merge_resolved(
        self,
        variables: list[EnvironmentVariable],
        loaded: dict[str, str],
    ) -> dict[str, str]:
        """Merge secrets and literal values into one env dict; missing refs raise ``KeyError``."""
        resolved: dict[str, str] = {}
        for variable in variables:
            if variable.secret_ref:
                value = loaded.get(variable.secret_ref)
                if value is None:
                    raise KeyError(f"missing secret for ref {variable.secret_ref}")
                resolved[variable.name] = value
                self._resolved[variable.secret_ref] = value
                continue
            if variable.value is not None:
                resolved[variable.name] = variable.value
        return resolved

    def known_secrets(self) -> dict[str, str]:
        """Snapshot every secret value resolved so far, keyed by secret ref."""
        return dict(self._resolved)

    def redactor(self) -> SecretRedactor:
        """Build a :class:`SecretRedactor` seeded with every resolved secret value."""
        return SecretRedactor(self.known_secrets())


__all__ = [
    "EnvSecretProvider",
    "SecretProvider",
    "SecretResolutionError",
    "SecretResolver",
    "normalize_secret_name",
    "resolve_async",
    "resolve_many_async",
    "resolve_secret_async",
]
