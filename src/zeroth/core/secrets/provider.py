"""Secret provider and resolution helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from zeroth.core.secrets.redaction import SecretRedactor

if TYPE_CHECKING:
    # Typed against execution_units.EnvironmentVariable without importing it at
    # runtime — execution_units.runner imports SecretResolver from here, so a
    # concrete import would create an import-time cycle.
    from zeroth.core.execution_units.models import EnvironmentVariable


class SecretProvider(Protocol):
    """Interface for resolving secret references to concrete values."""

    def resolve(self, secret_ref: str) -> str | None:  # pragma: no cover - protocol
        """Resolve a single secret reference."""

    def resolve_many(self, refs: list[str]) -> dict[str, str]:  # pragma: no cover - protocol
        """Resolve multiple secret references at once."""


class EnvSecretProvider:
    """Resolve secret references by reading process or injected environment variables."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = dict(environment or {})

    def resolve(self, secret_ref: str) -> str | None:
        return self._environment.get(secret_ref)

    def resolve_many(self, refs: list[str]) -> dict[str, str]:
        return {ref: value for ref in refs if (value := self.resolve(ref)) is not None}


class SecretResolver:
    """Resolve environment-variable models into a concrete runtime environment."""

    def __init__(self, provider: SecretProvider) -> None:
        self.provider = provider
        self._resolved: dict[str, str] = {}

    def resolve_environment_variables(
        self,
        variables: list[EnvironmentVariable],
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        refs = [item.secret_ref for item in variables if item.secret_ref]
        loaded = self.provider.resolve_many([ref for ref in refs if ref is not None])
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
        return dict(self._resolved)

    def redactor(self) -> SecretRedactor:
        return SecretRedactor(self.known_secrets())


__all__ = ["EnvSecretProvider", "SecretProvider", "SecretResolver"]
