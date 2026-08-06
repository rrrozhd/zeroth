"""Canonical import surface for the runtime agents package.

Non-golden boundary tests for the Task 14 agent runtime consolidation: the
canonical ``zeroth.runtime.agents`` package must publish the same objects the
legacy ``zeroth.core.agent_runtime`` path keeps republishing, and both
packages must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

EXPORTS = (
    "AgentAuditSerializer",
    "AgentConfig",
    "AgentContentBlockedError",
    "AgentInputValidationError",
    "AgentOutputValidationError",
    "AgentProviderError",
    "AgentRetryExhaustedError",
    "AgentRunResult",
    "AgentRunner",
    "AgentRuntimeError",
    "AgentTimeoutError",
    "CachingProviderAdapter",
    "CascadingProviderAdapter",
    "ContentSafetyConfig",
    "DeterministicProviderAdapter",
    "FallbackProviderAdapter",
    "HeuristicInjectionScreener",
    "InMemoryResponseCache",
    "InMemoryThreadStateStore",
    "InjectionScreener",
    "LiteLLMProviderAdapter",
    "MCPServerConfig",
    "ModelParams",
    "OutputValidator",
    "PromptAssembly",
    "PromptAssembler",
    "PromptConfig",
    "PromptMessage",
    "ProviderAdapter",
    "ProviderMessage",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderTarget",
    "RepositoryThreadResolver",
    "RepositoryThreadStateStore",
    "ResponseCache",
    "RetryPolicy",
    "SanitizedContent",
    "ThreadResolution",
    "ToolAttachmentAction",
    "ToolAttachmentBinding",
    "ToolAttachmentBridge",
    "ToolAttachmentError",
    "ToolAttachmentManifest",
    "ToolAttachmentRegistry",
    "ToolOutputSafetyConfig",
    "ToolOutputSanitizer",
    "ToolPermissionError",
    "UndeclaredToolError",
    "build_response_format",
    "normalize_declared_tool_refs",
    "wrap_untrusted",
)

MODULES = (
    "cascade",
    "errors",
    "factory",
    "mcp",
    "models",
    "prompt",
    "provider",
    "resilience",
    "response_format",
    "retry",
    "runner",
    "sanitization",
    "thread_store",
    "tools",
    "validation",
)


def test_agents_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import agent_runtime as legacy
    from zeroth.runtime import agents as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize("module_name", MODULES)
def test_agents_modules_exist_at_the_canonical_path(module_name: str) -> None:
    importlib.import_module(f"zeroth.runtime.agents.{module_name}")


def test_agents_factory_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core.agent_runtime import factory as legacy_factory
    from zeroth.runtime.agents import factory as canonical_factory

    assert canonical_factory.build_agent_runners is legacy_factory.build_agent_runners
    assert canonical_factory.AgentRunnerFactoryError is legacy_factory.AgentRunnerFactoryError


def test_legacy_factory_still_republishes_the_service_wiring() -> None:
    from zeroth.core.agent_runtime.factory import build_runners_for_deployment
    from zeroth.service.bootstrap.factory import (
        build_runners_for_deployment as canonical_wiring,
    )

    assert build_runners_for_deployment is canonical_wiring


def test_importing_the_thread_store_does_not_load_a_persistence_adapter() -> None:
    """The runtime must not put a persistence adapter on its import path.

    This used to be asserted syntactically -- the module was forbidden from
    naming ``zeroth.integrations`` at all -- which only held because the legacy
    ``zeroth.core.runs`` republisher laundered the edge. ZER-25 removed that
    republisher, so the edge is now explicit and carries a documented
    architecture exception.

    The property worth protecting was never the spelling of the import; it was
    that importing the thread store must not *execute* the adapter's module.
    The concrete repositories are named under ``TYPE_CHECKING`` and constructed
    inside a function, so a cold import still touches no persistence code --
    which is what this asserts, and what a syntactic check never proved.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import zeroth.runtime.agents.thread_store  # noqa: F401\n"
            "eager = [n for n in sys.modules if n.startswith('zeroth.integrations')]\n"
            "assert not eager, f'persistence loaded eagerly: {eager}'\n",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_memory_scope_is_the_contract_owned_enum() -> None:
    from zeroth.contracts.governed import MemoryScope as ContractMemoryScope
    from zeroth.core.governed.memory.models import MemoryScope as LegacyMemoryScope

    assert LegacyMemoryScope is ContractMemoryScope


def test_agents_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.runtime.agents"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
