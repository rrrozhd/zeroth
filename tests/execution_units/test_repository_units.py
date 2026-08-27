"""ZER-37 phase 5: repository manifests become executable units.

Covers the translation layer (`build_repository_manifest` /
`build_repository_binding`), the manifest-ref scheme, the validator branch for
:class:`RepositoryUnitManifest`, the runner's checkout-materializer seam
(fail-closed without one; staged tree lands under ``checkout/`` and rides
read-only with one), admission control keyed by the manifest digest, and the
smoke-assertion evaluator.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zeroth.contracts.repo_manifest import (
    RepoManifestDocument,
    RepoUnitPolicy,
    parse_manifest_document,
)
from zeroth.integrations.execution.integrity import (
    AdmissionController,
    compute_manifest_digest,
)
from zeroth.integrations.execution.models import (
    REPOSITORY_CHECKOUT_DIRNAME,
    ExecutionMode,
    RepositoryCheckoutArtifactSource,
    RepositoryUnitManifest,
)
from zeroth.integrations.execution.repo_units import (
    SmokeOutcome,
    build_repository_binding,
    build_repository_manifest,
    build_repository_manifest_ref,
    evaluate_smoke_assertions,
    manifest_config_digest,
    parse_repository_manifest_ref,
)
from zeroth.integrations.execution.runner import (
    ExecutableUnitAdmissionError,
    ExecutableUnitExecutionError,
    ExecutableUnitRunner,
)
from zeroth.integrations.execution.validator import (
    ExecutableUnitValidator,
    ValidationCode,
)
from zeroth.integrations.github.materializer import LocalCheckoutMaterializer
from zeroth.integrations.github.models import StagedCheckout

COMMIT_SHA = "a" * 40
TREE_DIGEST = "sha256:" + "b" * 64
CONFIG_DIGEST = "sha256:" + "c" * 64
POLICY = RepoUnitPolicy()


def _staged() -> StagedCheckout:
    return StagedCheckout(
        checkout_id="co-0001",
        commit_sha=COMMIT_SHA,
        git_tree_id="d" * 40,
        tree_digest=TREE_DIGEST,
        file_count=2,
        size_bytes=64,
        has_lfs_pointers=False,
        verified_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


def _document(text: str) -> RepoManifestDocument:
    document, report = parse_manifest_document(textwrap.dedent(text).encode())
    assert document is not None, report
    return document


def _manifest(
    document: RepoManifestDocument | None = None,
    *,
    script_name: str = "train",
    policy: RepoUnitPolicy = POLICY,
) -> RepositoryUnitManifest:
    if document is None:
        document = _document(
            """\
            schema_version: 1
            scripts:
              train:
                entry: scripts/train.py
                runtime: python3
            """
        )
    return build_repository_manifest(
        document,
        script_name=script_name,
        staged=_staged(),
        repository_id=4242,
        installation_id=8891,
        config_digest=CONFIG_DIGEST,
        policy=policy,
    )


class _StagedTreeMaterializer:
    """Test double for the runner seam: copies a prepared staged tree.

    Mirrors what the LOCAL/DOCKER wiring does — the staged root is resolved
    out-of-band from the artifact source's identities, then copied with the
    symlink-refusing local materializer.
    """

    def __init__(self, staged_root: Path) -> None:
        self._staged_root = staged_root
        self.calls: list[tuple[RepositoryCheckoutArtifactSource, Path]] = []

    async def materialize(
        self, source: RepositoryCheckoutArtifactSource, destination: Path
    ) -> None:
        self.calls.append((source, destination))
        LocalCheckoutMaterializer().materialize(self._staged_root, destination)


def _staged_tree(tmp_path: Path) -> Path:
    """A benign staged checkout: scripts/train.py echoes its JSON stdin."""
    root = tmp_path / "staged"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "train.py").write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        'print(json.dumps({"echo": payload["word"], "ok": True}))\n',
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# manifest_ref scheme
# ---------------------------------------------------------------------------


def test_manifest_ref_round_trips_through_the_parser() -> None:
    ref = build_repository_manifest_ref(
        installation_id=8891,
        repository_id=4242,
        commit_sha=COMMIT_SHA,
        script_name="train",
        config_digest=CONFIG_DIGEST,
    )

    assert ref == f"repo://8891/4242@{COMMIT_SHA}/train?cfg={'c' * 64}"
    parts = parse_repository_manifest_ref(ref)
    assert parts is not None
    assert parts.installation_id == 8891
    assert parts.repository_id == 4242
    assert parts.commit_sha == COMMIT_SHA
    assert parts.script_name == "train"
    assert parts.config_digest == CONFIG_DIGEST


@pytest.mark.parametrize(
    "ref",
    [
        "repo://8891/4242@nothex/train?cfg=" + "c" * 64,
        "repo://8891/4242@" + "a" * 40 + "/train",
        "eu://8891/4242@" + "a" * 40 + "/train?cfg=" + "c" * 64,
        "repo://x/4242@" + "a" * 40 + "/train?cfg=" + "c" * 64,
    ],
)
def test_manifest_ref_parser_refuses_malformed_refs(ref: str) -> None:
    assert parse_repository_manifest_ref(ref) is None


def test_config_digest_is_prefixed_sha256_of_the_raw_bytes() -> None:
    digest = manifest_config_digest(b"schema_version: 1\n")

    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert digest == manifest_config_digest(b"schema_version: 1\n")
    assert digest != manifest_config_digest(b"schema_version: 2\n")


# ---------------------------------------------------------------------------
# layout: working directory under checkout/, entry relative to it
# ---------------------------------------------------------------------------


def test_layout_places_the_root_workdir_at_checkout() -> None:
    manifest = _manifest()

    assert manifest.run_config.working_directory == REPOSITORY_CHECKOUT_DIRNAME
    assert manifest.run_config.command == ["python3", "-I", "scripts/train.py"]
    assert manifest.unit_id == "repo:4242:train"
    assert manifest.version == 1
    assert manifest.onboarding_mode is ExecutionMode.REPOSITORY


def test_layout_nested_workdir_gets_an_entry_relative_to_it() -> None:
    document = _document(
        """\
        schema_version: 1
        scripts:
          train:
            entry: app/main.py
            runtime: python3
            working_directory: app
        """
    )
    manifest = _manifest(document)

    assert manifest.run_config.working_directory == "checkout/app"
    assert manifest.run_config.command == ["python3", "-I", "main.py"]


def test_layout_entry_outside_the_workdir_uses_parent_segments() -> None:
    document = _document(
        """\
        schema_version: 1
        scripts:
          train:
            entry: tools/run.py
            runtime: python3
            working_directory: app
        """
    )
    manifest = _manifest(document)

    assert manifest.run_config.working_directory == "checkout/app"
    assert manifest.run_config.command == ["python3", "-I", "../tools/run.py"]


def test_build_refuses_a_script_the_document_does_not_declare() -> None:
    with pytest.raises(ValueError, match="not declared"):
        _manifest(script_name="deploy")


# ---------------------------------------------------------------------------
# policy-default inheritance
# ---------------------------------------------------------------------------


def test_absent_resources_inherit_the_policy_ceilings() -> None:
    manifest = _manifest()
    limits = manifest.resource_limits

    assert limits.cpu_cores == POLICY.max_cpu_cores
    assert limits.memory_mb == POLICY.max_memory_mb
    assert limits.timeout_seconds == POLICY.max_timeout_seconds
    assert limits.max_processes == POLICY.max_processes
    assert limits.network_access is False
    assert manifest.timeout_seconds == POLICY.max_timeout_seconds


def test_declared_resources_and_full_network_are_kept() -> None:
    document = _document(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
            resources:
              cpu_cores: 0.5
              memory_mb: 256
              timeout_seconds: 30
              max_processes: 8
            network:
              access: full
            environment:
              MODEL_NAME: small
            capabilities: [artifact_read]
        """
    )
    manifest = _manifest(document, policy=RepoUnitPolicy(allow_network=True))
    limits = manifest.resource_limits

    assert limits.cpu_cores == 0.5
    assert limits.memory_mb == 256
    assert limits.timeout_seconds == 30
    assert limits.max_processes == 8
    assert limits.network_access is True
    assert manifest.run_config.environment == {"MODEL_NAME": "small"}
    assert manifest.capability_requests == ["artifact_read"]


# ---------------------------------------------------------------------------
# validator branch
# ---------------------------------------------------------------------------


def _codes(manifest: RepositoryUnitManifest) -> list[ValidationCode]:
    return [issue.code for issue in ExecutableUnitValidator().validate(manifest).issues]


def test_validator_accepts_a_translated_manifest() -> None:
    report = ExecutableUnitValidator().validate(_manifest())

    assert report.is_valid, report.issues


def test_validator_flags_a_malformed_tree_digest_ref() -> None:
    manifest = _manifest().model_copy(
        update={
            "artifact_source": RepositoryCheckoutArtifactSource(
                ref="not-a-digest",
                commit_sha=COMMIT_SHA,
                config_digest=CONFIG_DIGEST,
                repository_id=4242,
                installation_id=8891,
            )
        }
    )

    assert ValidationCode.INVALID_ARTIFACT_SOURCE in _codes(manifest)


def test_validator_flags_a_malformed_commit_sha() -> None:
    manifest = _manifest().model_copy(
        update={
            "artifact_source": RepositoryCheckoutArtifactSource(
                ref=TREE_DIGEST,
                commit_sha="HEAD",
                config_digest=CONFIG_DIGEST,
                repository_id=4242,
                installation_id=8891,
            )
        }
    )

    assert ValidationCode.INVALID_ARTIFACT_SOURCE in _codes(manifest)


def test_validator_flags_a_malformed_config_digest() -> None:
    manifest = _manifest().model_copy(
        update={
            "artifact_source": RepositoryCheckoutArtifactSource(
                ref=TREE_DIGEST,
                commit_sha=COMMIT_SHA,
                config_digest="sha256:short",
                repository_id=4242,
                installation_id=8891,
            )
        }
    )

    assert ValidationCode.INVALID_ARTIFACT_SOURCE in _codes(manifest)


def test_validator_requires_a_run_command() -> None:
    manifest = _manifest()
    manifest = manifest.model_copy(
        update={"run_config": manifest.run_config.model_copy(update={"command": []})}
    )

    assert ValidationCode.MISSING_COMMAND in _codes(manifest)


def test_validator_requires_python3_as_the_interpreter() -> None:
    manifest = _manifest()
    manifest = manifest.model_copy(
        update={
            "run_config": manifest.run_config.model_copy(
                update={"command": ["bash", "-c", "true"]}
            )
        }
    )

    assert ValidationCode.INVALID_RUN_COMMAND in _codes(manifest)


# ---------------------------------------------------------------------------
# binding synthesis
# ---------------------------------------------------------------------------


def test_binding_carries_the_repo_scheme_ref_and_env_allowlist() -> None:
    document = _document(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
            environment:
              MODEL_NAME: small
              DATASET: tiny
        """
    )
    manifest = _manifest(document)
    binding = build_repository_binding(manifest)

    assert binding.manifest_ref == build_repository_manifest_ref(
        installation_id=8891,
        repository_id=4242,
        commit_sha=COMMIT_SHA,
        script_name="train",
        config_digest=CONFIG_DIGEST,
    )
    assert binding.manifest is manifest
    assert binding.python_handler is None
    # The defaults the sandbox environment builder expects, plus the manifest's
    # own environment keys.
    assert {"PATH", "HOME", "TMPDIR"} <= set(binding.allowed_env_keys)
    assert {"MODEL_NAME", "DATASET"} <= set(binding.allowed_env_keys)


def test_binding_models_accept_freeform_payloads() -> None:
    binding = build_repository_binding(_manifest())

    payload = binding.input_model.model_validate({"anything": ["goes", 1]})
    assert payload.model_dump() == {"anything": ["goes", 1]}


# ---------------------------------------------------------------------------
# runner seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_fails_closed_without_a_checkout_materializer() -> None:
    binding = build_repository_binding(_manifest())

    with pytest.raises(ExecutableUnitExecutionError, match="checkout materializer"):
        await ExecutableUnitRunner().run_binding(binding, {"word": "hi"})


@pytest.mark.asyncio
async def test_runner_materializes_into_checkout_before_running_read_only(
    tmp_path: Path,
) -> None:
    from unittest.mock import Mock

    from zeroth.integrations.execution.sandbox import (
        SandboxConfig,
        SandboxManager,
        SandboxStrictnessMode,
    )

    binding = build_repository_binding(_manifest())
    materializer = _StagedTreeMaterializer(_staged_tree(tmp_path))
    # Permissive up front (rather than only via the enforcement override) so the
    # runner reuses this exact manager instance and the spy observes the call.
    manager = SandboxManager(
        config=SandboxConfig(strictness_mode=SandboxStrictnessMode.PERMISSIVE)
    )
    runner = ExecutableUnitRunner(sandbox_manager=manager)
    runner.checkout_materializer = materializer
    local_spy = Mock(wraps=manager._run_locally)
    manager._run_locally = local_spy  # type: ignore[method-assign]

    # Inherited policy ceilings demand hard isolation, which the LOCAL backend
    # refuses under STANDARD strictness — the dev-mode escape hatch is the
    # explicit permissive override, exactly as a local deployment configures it.
    result = await runner.run_binding(
        binding,
        {"word": "hello"},
        enforcement_context={"sandbox_strictness_mode": "permissive"},
    )

    # The tree landed in <sandbox_root>/checkout before the command ran — the
    # command only succeeds because scripts/train.py was already in place.
    assert result.output_data == {"echo": "hello", "ok": True}
    (source, destination) = materializer.calls[0]
    assert source is binding.manifest.artifact_source
    assert destination.name == REPOSITORY_CHECKOUT_DIRNAME
    assert local_spy.call_count == 1
    assert local_spy.call_args.kwargs["read_only_paths"] == (REPOSITORY_CHECKOUT_DIRNAME,)
    assert result.audit_record["execution_mode"] == "repository"


@pytest.mark.asyncio
async def test_admission_passes_a_registered_digest_and_denies_a_mutated_manifest(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    binding = build_repository_binding(manifest)
    controller = AdmissionController()
    # admit() resolves trusted digests by the manifest's unit_id.
    controller.register_trusted_digest(manifest.unit_id, compute_manifest_digest(manifest))
    runner = ExecutableUnitRunner(admission_controller=controller)
    runner.checkout_materializer = _StagedTreeMaterializer(_staged_tree(tmp_path))

    permissive = {"sandbox_strictness_mode": "permissive"}
    result = await runner.run_binding(binding, {"word": "ok"}, enforcement_context=permissive)
    assert result.output_data == {"echo": "ok", "ok": True}

    mutated = manifest.model_copy(
        update={
            "run_config": manifest.run_config.model_copy(
                update={"environment": {"INJECTED": "1"}}
            )
        }
    )
    with pytest.raises(ExecutableUnitAdmissionError) as denial:
        await runner.run_binding(
            build_repository_binding(mutated), {"word": "no"}, enforcement_context=permissive
        )
    assert denial.value.audit_record["reason_code"] == "trusted_digest_mismatch"


# ---------------------------------------------------------------------------
# smoke evaluator
# ---------------------------------------------------------------------------


def _script_spec(text: str) -> object:
    document = _document(text)
    return document.scripts["train"]


def test_smoke_defaults_pass_on_exit_zero_and_fail_otherwise() -> None:
    spec = _script_spec(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
        """
    )

    assert evaluate_smoke_assertions(spec, exit_code=0, stdout_text="") == SmokeOutcome(
        passed=True
    )
    assert evaluate_smoke_assertions(spec, exit_code=3, stdout_text="") == SmokeOutcome(
        passed=False, failed_check="exit_code"
    )


def test_smoke_checks_declared_exit_code_and_stdout_substring() -> None:
    spec = _script_spec(
        """\
        schema_version: 1
        scripts:
          train:
            entry: scripts/train.py
            runtime: python3
            smoke:
              exit_code: 2
              stdout_contains: ready
        """
    )

    assert evaluate_smoke_assertions(spec, exit_code=2, stdout_text="model ready\n") == (
        SmokeOutcome(passed=True)
    )
    assert evaluate_smoke_assertions(spec, exit_code=0, stdout_text="model ready\n") == (
        SmokeOutcome(passed=False, failed_check="exit_code")
    )
    outcome = evaluate_smoke_assertions(spec, exit_code=2, stdout_text="secret-token")
    assert outcome == SmokeOutcome(passed=False, failed_check="stdout_contains")
    # The outcome never carries stdout content, only the name of the check.
    assert "secret-token" not in repr(outcome)
