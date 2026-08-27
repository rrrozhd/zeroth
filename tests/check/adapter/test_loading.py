from __future__ import annotations

import multiprocessing
from pathlib import Path
from queue import Empty

import pytest

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.loading import TargetLoadError, load_target


class Repository:
    pass


def _child(queue: multiprocessing.Queue, database: str) -> None:
    bindings = CheckBindings(action_repository=Repository())
    target = load_target("tests.check.fixtures.targets.payment:build_target", bindings)
    result = target.invoke(case="4", scenario_run_id="logical-1", checkpointer_path=database)
    queue.put((result, list(bindings.registrations), target.entrypoint_digest))


def test_loads_freezes_and_invokes_real_compiled_graph(tmp_path) -> None:
    bindings = CheckBindings(action_repository=Repository())
    target = load_target("tests.check.fixtures.targets.payment:build_target", bindings)
    result = target.invoke(
        case="4", scenario_run_id="logical-1", checkpointer_path=tmp_path / "checkpoint.sqlite"
    )
    assert result == {"value": 5}
    assert target.entrypoint_digest.startswith("sha256:")
    with pytest.raises(Exception, match="frozen"):
        bindings.tool("other", lambda value: value, "read_only")


def test_rebuilds_in_a_fresh_process_with_caller_owned_checkpoint(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_child, args=(queue, str(tmp_path / "child.sqlite")))
    process.start()
    process.join(15)
    assert process.exitcode == 0
    try:
        result, registrations, digest = queue.get(timeout=2)
    except Empty as exc:
        raise AssertionError("child returned no target result") from exc
    assert result == {"value": 5}
    assert registrations == ["increment"]
    assert digest.startswith("sha256:")
    assert (tmp_path / "child.sqlite").exists()


@pytest.mark.parametrize(
    "entrypoint",
    [
        "tests.check.fixtures.targets.invalid:build_target",
        "tests.check.fixtures.targets.payment:not_build_target",
        "bad-entrypoint",
    ],
)
def test_rejects_invalid_target_contract(entrypoint: str) -> None:
    with pytest.raises(TargetLoadError):
        load_target(entrypoint, CheckBindings(action_repository=Repository()))
