"""ZER-37 AC#3: repository contents execute consistently across backends.

One repository-unit binding, one payload, two backends: (a) executed for real
on the LOCAL backend, capturing exactly what ``_run_locally`` was asked to do,
and (b) dispatched to a fake sidecar client capturing the
:class:`SidecarExecuteRequest`. The parity assertions pin, at the request
level, that the sidecar is asked to run the SAME semantics the local run used:
identical command tokens (repository commands carry no host paths, so the
host->/workspace rewrite is the identity here -- asserted explicitly), the
working directory is the /workspace twin of the local relative cwd, the
checkout subtree rides read-only on both, the JSON payload travels on stdin on
both, the environment is equal (no value references either sandbox root), the
resource ceilings transfer, and the uploaded workspace tar contains EXACTLY
the staged checkout tree -- paths, contents, and exec bits -- with no runner
IO files (v1 repository IO is json_stdin/json_stdout) and no host extras.

Real docker/sidecar execution is environment-dependent and deliberately stays
out of unit CI; this pins the contract the production backends receive.
"""

from __future__ import annotations

import json
import tarfile
import textwrap
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from tests.execution_units.test_sandbox_sidecar_dispatch import (
    _FakeSidecarClient,
    _response,
)
from zeroth.contracts.repo_manifest import RepoUnitPolicy, parse_manifest_document
from zeroth.integrations.execution.models import (
    REPOSITORY_CHECKOUT_DIRNAME,
    RepositoryCheckoutArtifactSource,
)
from zeroth.integrations.execution.repo_units import (
    build_repository_binding,
    build_repository_manifest,
)
from zeroth.integrations.execution.runner import ExecutableUnitRunner
from zeroth.integrations.execution.sandbox import (
    SandboxBackendMode,
    SandboxConfig,
    SandboxManager,
    SandboxStrictnessMode,
)
from zeroth.integrations.github.materializer import LocalCheckoutMaterializer
from zeroth.integrations.github.models import StagedCheckout

PERMISSIVE = {"sandbox_strictness_mode": "permissive"}

_MANIFEST = """\
schema_version: 1
scripts:
  train:
    entry: scripts/train.py
    runtime: python3
    environment:
      MODEL_NAME: parity
"""

_TRAIN_SCRIPT = """\
#!/usr/bin/env python3
import json
import os
import sys

payload = json.load(sys.stdin)
json.dump({"echo": payload["word"], "model": os.environ.get("MODEL_NAME"), "ok": True}, sys.stdout)
"""


def _staged_tree(tmp_path: Path) -> Path:
    """A staged checkout with an executable entry and a plain data file."""
    root = tmp_path / "staged"
    (root / "scripts").mkdir(parents=True)
    train = root / "scripts" / "train.py"
    train.write_text(_TRAIN_SCRIPT, encoding="utf-8")
    train.chmod(0o755)
    (root / "data").mkdir()
    (root / "data" / "config.json").write_text('{"name": "parity"}\n', encoding="utf-8")
    return root


def _binding():
    document, report = parse_manifest_document(textwrap.dedent(_MANIFEST).encode())
    assert document is not None, report
    manifest = build_repository_manifest(
        document,
        script_name="train",
        staged=StagedCheckout(
            checkout_id="co-parity",
            commit_sha="a" * 40,
            git_tree_id="d" * 40,
            tree_digest="sha256:" + "b" * 64,
            file_count=2,
            size_bytes=128,
            has_lfs_pointers=False,
            verified_at=datetime(2026, 8, 26, tzinfo=UTC),
        ),
        repository_id=4242,
        installation_id=8891,
        config_digest="sha256:" + "c" * 64,
        policy=RepoUnitPolicy(),
    )
    return build_repository_binding(manifest)


class _StagedTreeMaterializer:
    """Runner-seam double: copies the prepared staged tree, symlink-refusing."""

    def __init__(self, staged_root: Path) -> None:
        self._staged_root = staged_root

    async def materialize(
        self, source: RepositoryCheckoutArtifactSource, destination: Path
    ) -> None:
        del source
        LocalCheckoutMaterializer().materialize(self._staged_root, destination)


async def test_sidecar_request_preserves_the_local_run_semantics(tmp_path: Path) -> None:
    staged_root = _staged_tree(tmp_path)
    binding = _binding()
    payload = {"word": "hello"}

    # -- (a) the LOCAL backend, for real, with the invocation captured --------
    local_manager = SandboxManager(
        config=SandboxConfig(
            strictness_mode=SandboxStrictnessMode.PERMISSIVE,
            allow_untrusted_local_development=True,
        )
    )
    local_calls: list[dict[str, object]] = []
    original_run_locally = local_manager._run_locally

    def recording_run_locally(**kwargs):  # noqa: ANN003, ANN202
        local_calls.append(kwargs)
        return original_run_locally(**kwargs)

    local_manager._run_locally = recording_run_locally  # type: ignore[method-assign]
    local_runner = ExecutableUnitRunner(sandbox_manager=local_manager)
    local_runner.checkout_materializer = _StagedTreeMaterializer(staged_root)

    local_result = await local_runner.run_binding(
        binding, payload, enforcement_context=dict(PERMISSIVE)
    )

    assert local_result.output_data == {"echo": "hello", "model": "parity", "ok": True}
    assert local_result.sandbox_result is not None
    assert local_result.sandbox_result.backend == "local"
    assert len(local_calls) == 1
    local_kwargs = local_calls[0]
    local_cwd = Path(str(local_kwargs["cwd"]))
    assert local_cwd.name == REPOSITORY_CHECKOUT_DIRNAME
    local_root = local_cwd.parent
    local_relative_cwd = local_cwd.relative_to(local_root).as_posix()
    local_env = dict(local_kwargs["environment"].variables)  # type: ignore[union-attr]
    local_command = [str(token) for token in local_kwargs["command"]]  # type: ignore[union-attr]
    local_constraints = local_kwargs["resource_constraints"]
    assert local_constraints is not None

    # -- (b) the SAME binding dispatched to a captured sidecar request --------
    client = _FakeSidecarClient(
        response=_response(stdout=local_result.sandbox_result.stdout, returncode=0)
    )
    sidecar_manager = SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.SIDECAR,
            strictness_mode=SandboxStrictnessMode.PERMISSIVE,
        ),
        sidecar_client=client,
    )
    sidecar_runner = ExecutableUnitRunner(sandbox_manager=sidecar_manager)
    sidecar_runner.checkout_materializer = _StagedTreeMaterializer(staged_root)

    sidecar_result = await sidecar_runner.run_binding(
        binding, payload, enforcement_context=dict(PERMISSIVE)
    )

    assert [kind for kind, _ in client.calls] == ["upload", "execute"]
    request = client.requests[0]

    # Same command tokens: a repository run command is host-path-free by
    # construction (entry relative to the checkout workdir), so the mandated
    # host->/workspace rewrite is the identity -- assert both halves.
    assert local_command == ["python3", "-I", "scripts/train.py"]
    assert request.command == local_command
    assert not any(str(local_root) in token for token in request.command)

    # Working directory: the /workspace twin of the local relative cwd.
    assert local_relative_cwd == REPOSITORY_CHECKOUT_DIRNAME
    assert request.working_directory == f"/workspace/{local_relative_cwd}"

    # The checkout subtree rides read-only on both backends.
    assert tuple(local_kwargs["read_only_paths"]) == (REPOSITORY_CHECKOUT_DIRNAME,)  # type: ignore[arg-type]
    assert request.read_only_paths == [REPOSITORY_CHECKOUT_DIRNAME]

    # The payload travels on stdin on both backends, byte-identical.
    assert request.input_text == local_kwargs["input_text"]
    assert json.loads(str(request.input_text)) == payload

    # Environment equal modulo path rewrite -- and since no value references
    # either sandbox root (asserted), "modulo rewrite" collapses to equality.
    assert not any(str(local_root) in value for value in local_env.values())
    assert not any(str(local_root) in value for value in request.environment.values())
    assert request.environment == local_env
    assert request.environment["MODEL_NAME"] == "parity"

    # The manifest's policy-inherited resource ceilings transfer to the request.
    assert request.cpu_cores == local_constraints.cpu_cores  # type: ignore[union-attr]
    assert request.memory_mb == local_constraints.memory_mb  # type: ignore[union-attr]
    assert request.max_processes == local_constraints.max_processes  # type: ignore[union-attr]
    assert request.network_access is False

    # The uploaded tar is EXACTLY the staged checkout tree: same paths, same
    # bytes, exec bit preserved on the entry, nothing else -- no runner IO
    # files (v1 repository IO is stdin/stdout) and no host extras.
    with tarfile.open(fileobj=BytesIO(client.uploads[0][1]), mode="r:") as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert set(members) == {
            "checkout",
            "checkout/data",
            "checkout/scripts",
            "checkout/data/config.json",
            "checkout/scripts/train.py",
        }
        assert members["checkout"].isdir()
        assert members["checkout/scripts/train.py"].isreg()
        assert members["checkout/scripts/train.py"].mode & 0o111, "exec bit must survive"
        assert not members["checkout/data/config.json"].mode & 0o111
        for name, staged_file in (
            ("checkout/scripts/train.py", staged_root / "scripts" / "train.py"),
            ("checkout/data/config.json", staged_root / "data" / "config.json"),
        ):
            extracted = archive.extractfile(name)
            assert extracted is not None
            assert extracted.read() == staged_file.read_bytes()

    # And the observable outcome is the same run: replaying the local stdout
    # through the sidecar response yields the identical extracted output.
    assert sidecar_result.output_data == local_result.output_data
    assert sidecar_result.sandbox_result is not None
    assert sidecar_result.sandbox_result.backend == "sidecar"
