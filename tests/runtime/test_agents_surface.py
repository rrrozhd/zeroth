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


def test_thread_store_reaches_persistence_only_through_the_legacy_republisher() -> None:
    """The canonical thread store must not import zeroth.integrations directly."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ast, pathlib, sys\n"
            "import zeroth.runtime.agents.thread_store as mod\n"
            "tree = ast.parse(pathlib.Path(mod.__file__).read_text())\n"
            "names = []\n"
            "for node in ast.walk(tree):\n"
            "    if isinstance(node, ast.ImportFrom) and node.module:\n"
            "        names.append(node.module)\n"
            "    elif isinstance(node, ast.Import):\n"
            "        names.extend(alias.name for alias in node.names)\n"
            "bad = [n for n in names if n.startswith('zeroth.integrations')]\n"
            "assert not bad, f'direct integrations imports: {bad}'\n",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_memory_scope_is_the_contract_owned_enum() -> None:
    from zeroth.contracts.governed import MemoryScope as ContractMemoryScope
    from zeroth.core.governed.memory.models import MemoryScope as LegacyMemoryScope

    assert LegacyMemoryScope is ContractMemoryScope


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.runtime.agents", "zeroth.core.agent_runtime"),
        ("zeroth.core.agent_runtime", "zeroth.runtime.agents"),
    ],
)
def test_agents_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
