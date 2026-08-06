"""Subprocess cold-import guard for the canonical service packages.

``tests/conftest.py`` imports service bootstrap at collection time, so every
in-process test runs with most of the service graph already warm and a circular
import between these packages is structurally invisible to the suite. A library
consumer has no such warm cache, so the closure must stand up cold.

Partial-initialization cycles are order-dependent, so the modules are imported
both in relocation order and in reverse: the first import in each subprocess is
the genuinely cold probe, and the rest assert the closure stays consistent once
one side is warm. ZER-25 removed the legacy half of this guard along with the
``zeroth.core.service`` shims; reversing the canonical list preserves what the
legacy-first direction was actually testing.
"""

from __future__ import annotations

import subprocess
import sys

# Canonical service modules, in the order Task 10 relocated them.
CANONICAL_SERVICE_MODULES = [
    "zeroth.service.api.studio_schemas",
    "zeroth.service.bootstrap.configuration",
    "zeroth.service.bootstrap.migrations",
    "zeroth.service.bootstrap.container",
    "zeroth.service.bootstrap.factory",
    "zeroth.service.api.authorization",
    "zeroth.service.api.authentication",
    "zeroth.service.api.artifact_api",
    "zeroth.service.api.run_api",
    "zeroth.service.api.contracts_api",
    "zeroth.service.api.manifest_api",
    "zeroth.service.api.econ_analytics_api",
    "zeroth.service.api.admin_api",
    "zeroth.service.api.cost_api",
    "zeroth.service.api.rightsizing_api",
    "zeroth.service.api.template_api",
    "zeroth.service.api.deployment_api",
    "zeroth.service.api.approval_api",
    "zeroth.service.api.connector_api",
    "zeroth.service.api.webhook_api",
    "zeroth.service.api.retention_api",
    "zeroth.service.api.audit_api",
    "zeroth.service.api.studio_api",
    "zeroth.service.api.health",
    "zeroth.service.api.console_ui",
    "zeroth.service.bootstrap.lifecycle",
    "zeroth.service.app",
    "zeroth.service.entrypoint",
    "zeroth.service.bootstrap",
    "zeroth.service",
]


def _import_all(module_names: list[str]) -> subprocess.CompletedProcess[str]:
    code = "\n".join(f"import {name}" for name in module_names)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_canonical_service_modules_import_in_a_cold_interpreter() -> None:
    """Relocation order: no module may need another pre-warmed."""
    result = _import_all(CANONICAL_SERVICE_MODULES)
    assert result.returncode == 0, f"cold import in relocation order failed:\n{result.stderr}"


def test_canonical_service_modules_import_in_reverse_order() -> None:
    """Reverse order: the closure holds entering from the other side too.

    A cycle between two of these packages shows up only when the *other* one is
    imported first, which is why the legacy-first direction existed. With the
    shims gone, reversing the canonical list probes the same asymmetry.
    """
    result = _import_all(list(reversed(CANONICAL_SERVICE_MODULES)))
    assert result.returncode == 0, f"cold import in reverse order failed:\n{result.stderr}"


def test_the_service_package_init_is_lazy() -> None:
    """Importing ``zeroth.service`` must not execute the app or bootstrap.

    The package init is the re-entry point for the cycle class this module
    guards: if it eagerly imported ``.bootstrap`` or ``.app``, any module that
    touches ``zeroth.service`` would re-enter a partially initialized module.
    Laziness here is load-bearing, and it outlived the shims it was written for.
    """
    code = (
        "import sys\n"
        "import zeroth.service\n"
        "assert 'zeroth.service.app' not in sys.modules, 'app imported eagerly'\n"
        "assert 'zeroth.service.bootstrap' not in sys.modules, 'bootstrap imported eagerly'\n"
        "from zeroth.service import bootstrap_app, bootstrap_service, create_app\n"
        "from zeroth.service import DeploymentBootstrapError, ServiceBootstrap\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, f"lazy service init broken:\n{result.stderr}"
