"""Characterization tests for the runtime-owned run and thread domain contracts.

``zeroth.runtime.runs`` is the canonical import location for the run, thread,
and checkpoint models plus the repository protocols runtime code depends on.
The concrete SQL-backed repositories stay outside the runtime domain; these
tests pin the contract they must keep satisfying.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from typing import Any, Protocol, get_type_hints

import pytest

from zeroth.core.runs.repository import RunRepository, ThreadRepository
from zeroth.runtime.runs import (
    CheckpointStore,
    Run,
    RunConditionResult,
    RunFailureState,
    RunHistoryEntry,
    RunReader,
    RunStatus,
    RunWriter,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
    ThreadStore,
)


CANONICAL_MODELS = (
    "Run",
    "RunConditionResult",
    "RunFailureState",
    "RunHistoryEntry",
    "RunStatus",
    "Thread",
    "ThreadMemoryBinding",
    "ThreadStatus",
)


def test_canonical_package_imports_in_a_cold_interpreter() -> None:
    """``zeroth.runtime.runs`` must import without ``zeroth.core`` being loaded first.

    This runs in a subprocess on purpose. ``tests/conftest.py`` imports
    ``zeroth.core.service.bootstrap`` at collection time, so by the time any
    in-process test runs, ``zeroth.core`` is already in ``sys.modules`` and a
    circular import between the canonical package and ``zeroth.core`` is
    invisible. A library consumer following the migration guide has no such
    warm cache: ``from zeroth.runtime.runs import Run`` may be the very first
    ``zeroth`` import in the process.

    ``zeroth.runtime.runs`` re-exports models defined under ``zeroth.core``,
    and importing anything from ``zeroth.core`` executes its eager package
    ``__init__``. So no module reachable from ``zeroth.core.__init__`` may
    import ``zeroth.runtime.runs`` while that remains true.
    """
    result = subprocess.run(
        [sys.executable, "-c", "from zeroth.runtime.runs import Run, RunReader, ThreadStore"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"zeroth.runtime.runs is not cold-importable:\n{result.stderr}"


@pytest.mark.parametrize("name", CANONICAL_MODELS)
def test_canonical_runs_package_publishes_every_run_domain_model(name: str) -> None:
    """Runtime code resolves the run domain models from one canonical package."""
    import zeroth.runtime.runs as canonical

    assert name in canonical.__all__
    assert hasattr(canonical, name)


@pytest.mark.parametrize("name", CANONICAL_MODELS)
def test_canonical_models_are_the_protected_model_objects(name: str) -> None:
    """The canonical package republishes the established models, not copies.

    Redefining these classes would change their ``__module__`` and therefore
    the ``inspect.signature`` strings pinned by the protected library surface.
    """
    import zeroth.core.runs as legacy
    import zeroth.runtime.runs as canonical

    assert getattr(canonical, name) is getattr(legacy, name)


@pytest.mark.parametrize("name", CANONICAL_MODELS)
def test_canonical_models_are_republished_rather_than_redefined(name: str) -> None:
    """The canonical package must not shadow the models with new definitions.

    Pinning each defining module keeps accidental shadowing out of the tree:
    a redefinition here would satisfy the identity-free checks while quietly
    forking the type. The allowed set names every deliberate owner —
    ``zeroth.core.runs.models`` for the run shapes, the governed bundle for
    ``RunState``/``RunStatus``, and ``zeroth.contracts.conditions.models``
    for ``RunConditionResult``, the conditions domain's evaluation-outcome
    vocabulary that the run surface republishes (Task 12).
    """
    import zeroth.runtime.runs as canonical

    model = getattr(canonical, name)

    assert model.__module__ in {
        "zeroth.core.runs.models",
        "zeroth.core.governed.models.common",
        "zeroth.contracts.conditions.models",
    }


def _protocol_members(protocol: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(protocol).items()
        if not name.startswith("_") and callable(member)
    }


def _assert_implements(protocol: type, implementation: type) -> None:
    """Assert ``implementation`` structurally satisfies every protocol member."""
    for name, expected in _protocol_members(protocol).items():
        actual_member = getattr(implementation, name, None)
        assert actual_member is not None, f"{implementation.__name__} lacks {name}"
        assert inspect.iscoroutinefunction(actual_member) is inspect.iscoroutinefunction(
            getattr(protocol, name)
        ), f"{implementation.__name__}.{name} awaitability differs from the protocol"

        actual = inspect.signature(actual_member)
        for parameter in expected.parameters.values():
            if parameter.name == "self":
                continue
            concrete = actual.parameters.get(parameter.name)
            assert concrete is not None, f"{implementation.__name__}.{name} lacks {parameter.name}"
            assert concrete.kind == parameter.kind, (
                f"{implementation.__name__}.{name}.{parameter.name} kind differs"
            )
            assert concrete.annotation == parameter.annotation, (
                f"{implementation.__name__}.{name}.{parameter.name} annotation differs"
            )
        extra = set(actual.parameters) - set(expected.parameters)
        for parameter_name in extra:
            assert actual.parameters[parameter_name].default is not inspect.Parameter.empty, (
                f"{implementation.__name__}.{name} requires unsupported {parameter_name}"
            )
        assert actual.return_annotation == expected.return_annotation, (
            f"{implementation.__name__}.{name} return annotation differs"
        )


@pytest.mark.parametrize(
    ("protocol", "expected_members"),
    [
        (RunReader, {"get"}),
        (RunWriter, {"create", "put"}),
        (CheckpointStore, {"get_checkpoint", "write_checkpoint"}),
        (ThreadStore, {"get", "resolve"}),
    ],
    ids=["RunReader", "RunWriter", "CheckpointStore", "ThreadStore"],
)
def test_each_protocol_declares_only_what_runtime_consumes(
    protocol: type, expected_members: set[str]
) -> None:
    """The protocols are derived from real runtime call sites, not the full repository API."""
    assert issubclass(protocol, Protocol)  # type: ignore[arg-type]
    assert set(_protocol_members(protocol)) == expected_members


@pytest.mark.parametrize(
    ("protocol", "implementation"),
    [
        (RunReader, RunRepository),
        (RunWriter, RunRepository),
        (CheckpointStore, RunRepository),
        (ThreadStore, ThreadRepository),
    ],
    ids=["RunReader", "RunWriter", "CheckpointStore", "ThreadStore"],
)
def test_concrete_repositories_satisfy_the_runtime_protocols(
    protocol: type, implementation: type
) -> None:
    """The shipped repositories remain valid implementations of the runtime contracts."""
    _assert_implements(protocol, implementation)


def test_protocols_are_annotated_with_canonical_run_domain_models() -> None:
    """Protocol annotations resolve to the canonical models, not restated shapes."""
    hints = get_type_hints(RunReader.get)

    assert hints["return"] == Run | None


async def test_structural_double_round_trips_a_run_through_the_protocols() -> None:
    """The narrow contract is sufficient to persist and reload a run."""

    class RecordingRunStore:
        def __init__(self) -> None:
            self.saved: dict[str, Run] = {}

        async def create(self, run: Run) -> Run:
            self.saved[run.run_id] = run
            return run

        async def put(self, run: Run) -> Run:
            self.saved[run.run_id] = run
            return run

        async def get(self, run_id: str) -> Run | None:
            return self.saved.get(run_id)

    store: Any = RecordingRunStore()
    run = Run(graph_version_ref="graph@1", deployment_ref="deploy@1")

    await store.create(run)
    loaded = await store.get(run.run_id)

    assert loaded is not None
    assert loaded.run_id == run.run_id
    assert loaded.status is RunStatus.PENDING


def test_thread_domain_models_remain_importable_from_the_canonical_package() -> None:
    """Thread state models travel with the run contracts they belong to."""
    thread = Thread(graph_version_ref="graph@1", deployment_ref="deploy@1")

    assert thread.status is ThreadStatus.ACTIVE
    assert thread.memory_bindings == []
    assert ThreadMemoryBinding(connector_id="c", instance_id="i").scope is None


def test_run_detail_models_remain_importable_from_the_canonical_package() -> None:
    """Per-node history, condition, and failure records are part of the run contract."""
    entry = RunHistoryEntry(node_id="n1", status="completed")
    condition = RunConditionResult(condition_id="c1", matched=True)
    failure = RunFailureState(reason="boom")

    assert entry.attempt == 1
    assert condition.selected_edge_id is None
    assert failure.message is None
