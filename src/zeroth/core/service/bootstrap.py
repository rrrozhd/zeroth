"""Legacy import location for the service bootstrap.

The implementation now lives in :mod:`zeroth.service.bootstrap`:
configuration defaults in ``configuration``, Alembic migrations in
``migrations``, the dependency container in ``container``, and the factory
in ``factory``. This module republishes the same objects so every legacy
import site — including the collection-time ``run_migrations`` import in
``tests/conftest.py`` — keeps resolving to identical objects.
"""

from __future__ import annotations

from zeroth.service.bootstrap.configuration import (
    _BootstrapMemorySettings as _BootstrapMemorySettings,
)
from zeroth.service.bootstrap.configuration import (
    _BootstrapMemorySubsection as _BootstrapMemorySubsection,
)
from zeroth.service.bootstrap.container import (
    DeploymentBootstrapError as DeploymentBootstrapError,
)
from zeroth.service.bootstrap.container import (
    ServiceBootstrap as ServiceBootstrap,
)
from zeroth.service.bootstrap.factory import bootstrap_app as bootstrap_app
from zeroth.service.bootstrap.factory import bootstrap_service as bootstrap_service
from zeroth.service.bootstrap.migrations import run_migrations as run_migrations

__all__ = [
    "DeploymentBootstrapError",
    "ServiceBootstrap",
    "bootstrap_app",
    "bootstrap_service",
    "run_migrations",
]
