"""Repository manifests against staged checkouts: validation and translation.

Two halves of the ZER-37 pipeline live here. The *filesystem* half:
:func:`zeroth.contracts.repo_manifest.parse_manifest_document` is pure and has
already bounded every path *syntactically*; :func:`validate_staged_manifest`
answers the questions only a real checkout can -- does the entry resolve
inside the staged root, is it a regular file, is the working directory a
directory, are the smoke files present. The *translation* half turns a
document that passed every gate into a :class:`RepositoryUnitManifest` and an
:class:`~zeroth.integrations.execution.runner.ExecutableUnitBinding` the
runner can execute, mirroring how :mod:`.inline` synthesizes per-run bindings.

The sandbox layout the translation commits to: the checkout tree materializes
into the ``checkout/`` subdirectory of the sandbox root
(:data:`~zeroth.integrations.execution.models.REPOSITORY_CHECKOUT_DIRNAME`),
the working directory is ``checkout/<document workdir>``, and the run command
names the entry *relative to that working directory* (``..`` segments are
legal -- containment was already validated against the staged tree). The
whole ``checkout/`` subtree is handed to execution as read-only: v1 manifests
only offer ``json_stdin``/``json_stdout``/``exit_code_only``, so no IO file is
ever written inside the tree.

Symlinks are handled in two layers, on purpose. Containment uses the resolving
:func:`~zeroth.platform.primitives.confine_path` /
:func:`~zeroth.platform.primitives.confine_directory` primitives, so a symlink
that resolves outside the checkout is an escape regardless of how it is
spelled. The entry file is then additionally required to be a regular file by
``lstat`` -- a symlink *inside* the checkout is refused too, because the staged
tree is attacker-authored and a link is a level of indirection the runner has
no reason to honor.

Messages never carry a path value taken from the document; the issue's
``path`` tuple (schema field names and indexes only) locates the offending
field, and the smoke outcome names only the check that failed, never what
stdout held.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from zeroth.contracts.repo_manifest import (
    NetworkAccess,
    RepoManifestDocument,
    RepoManifestIssue,
    RepoManifestSeverity,
    RepoManifestValidationCode,
    RepoManifestValidationReport,
    RepoUnitPolicy,
    ScriptSpec,
)
from zeroth.contracts.repo_manifest import (
    OutputMode as RepoOutputMode,
)
from zeroth.integrations.execution.inline import FreeformPayload
from zeroth.integrations.execution.models import (
    REPOSITORY_CHECKOUT_DIRNAME,
    InputMode,
    OutputMode,
    RepositoryCheckoutArtifactSource,
    RepositoryUnitManifest,
    ResourceLimits,
    RunConfig,
)
from zeroth.integrations.execution.runner import (
    _DEFAULT_ALLOWED_ENV_KEYS,
    ExecutableUnitBinding,
)
from zeroth.integrations.github.models import StagedCheckout
from zeroth.platform.primitives import (
    DestinationNotADirectoryError,
    OutboundDestinationError,
    confine_directory,
    confine_path,
)

__all__ = [
    "REPOSITORY_CONTRACT_REF",
    "CheckoutMaterializer",
    "RepositoryManifestRefParts",
    "SmokeOutcome",
    "build_repository_binding",
    "build_repository_manifest",
    "build_repository_manifest_ref",
    "evaluate_smoke_assertions",
    "manifest_config_digest",
    "parse_repository_manifest_ref",
    "validate_staged_manifest",
]

# Repository units accept whatever the graph routes to them, exactly like
# inline units: node-level contracts remain the typed boundary.
REPOSITORY_CONTRACT_REF = "contract://repo-freeform"


def _error(
    code: RepoManifestValidationCode, path: tuple[str, ...], message: str
) -> RepoManifestIssue:
    return RepoManifestIssue(
        severity=RepoManifestSeverity.ERROR, code=code, path=path, message=message
    )


def validate_staged_manifest(
    document: RepoManifestDocument, staged_root: Path
) -> RepoManifestValidationReport:
    """Check a parsed manifest's paths against the staged checkout it describes.

    Args:
        document: A document ``parse_manifest_document`` accepted.
        staged_root: The directory the repository was staged into.

    Returns:
        A report of every path that escapes the checkout, names the wrong kind
        of filesystem object, or is missing.
    """
    issues: list[RepoManifestIssue] = []
    resolved_root = staged_root.expanduser().resolve()

    for name in sorted(document.scripts):
        script = document.scripts[name]
        prefix = ("scripts", name)

        try:
            confine_path(script.entry, root=resolved_root, context="repository manifest entry")
        except OutboundDestinationError:
            issues.append(
                _error(
                    RepoManifestValidationCode.SCRIPT_PATH_ESCAPES_CHECKOUT,
                    (*prefix, "entry"),
                    "entry resolves outside the staged checkout",
                )
            )
        else:
            # lstat on the literal path: containment above already resolved
            # symlinks for the boundary question; this answers the shape
            # question -- the entry itself must be a regular file, not a link.
            literal_entry = resolved_root / script.entry
            try:
                entry_mode = literal_entry.lstat().st_mode
            except OSError:
                entry_mode = None
            if entry_mode is None or not stat.S_ISREG(entry_mode):
                issues.append(
                    _error(
                        RepoManifestValidationCode.SCRIPT_NOT_A_FILE,
                        (*prefix, "entry"),
                        "entry must name an existing regular file in the checkout",
                    )
                )

        try:
            confine_directory(
                script.working_directory,
                root=resolved_root,
                context="repository manifest working_directory",
            )
        except DestinationNotADirectoryError:
            issues.append(
                _error(
                    RepoManifestValidationCode.WORKDIR_NOT_A_DIRECTORY,
                    (*prefix, "working_directory"),
                    "working_directory must name an existing directory in the checkout",
                )
            )
        except OutboundDestinationError:
            issues.append(
                _error(
                    RepoManifestValidationCode.WORKDIR_ESCAPES_CHECKOUT,
                    (*prefix, "working_directory"),
                    "working_directory resolves outside the staged checkout",
                )
            )

        if script.smoke is not None:
            for index, smoke_file in enumerate(script.smoke.files_exist):
                issue_path = (*prefix, "smoke", "files_exist", str(index))
                try:
                    resolved = confine_path(
                        smoke_file, root=resolved_root, context="repository manifest smoke file"
                    )
                except OutboundDestinationError:
                    issues.append(
                        _error(
                            RepoManifestValidationCode.SMOKE_PATH_ESCAPES_CHECKOUT,
                            issue_path,
                            "smoke file resolves outside the staged checkout",
                        )
                    )
                else:
                    if not resolved.exists():
                        issues.append(
                            _error(
                                RepoManifestValidationCode.SMOKE_FILE_MISSING,
                                issue_path,
                                "smoke file is not present in the staged checkout",
                            )
                        )

    return RepoManifestValidationReport(issues=tuple(issues))


class CheckoutMaterializer(Protocol):
    """Places a verified checkout tree into a sandbox destination.

    The minimal seam the runner needs: given the manifest's artifact source
    (which pins installation, repository, commit, tree digest, and config
    digest), put that exact tree at ``destination``. The LOCAL and DOCKER
    wirings resolve the staged tree those identities name and copy it with
    :class:`zeroth.integrations.github.materializer.LocalCheckoutMaterializer`;
    the sidecar path instead stages the tree through the sidecar's workspace
    channel. The service layer wires the implementations -- the runner only
    fails closed when none was injected.
    """

    async def materialize(
        self, source: RepositoryCheckoutArtifactSource, destination: Path
    ) -> None:
        """Materialize the tree ``source`` identifies into ``destination``."""
        ...


def manifest_config_digest(raw_bytes: bytes) -> str:
    """Content digest of the raw ``.zeroth.yaml`` bytes, ``sha256:`` prefixed.

    Computed over the bytes exactly as fetched, before parsing, so two
    manifests that normalize alike but differ on the wire still get distinct
    identities.
    """
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class RepositoryManifestRefParts:
    """The identities a ``repo://`` manifest ref carries."""

    installation_id: int
    repository_id: int
    commit_sha: str
    script_name: str
    config_digest: str


def build_repository_manifest_ref(
    *,
    installation_id: int,
    repository_id: int,
    commit_sha: str,
    script_name: str,
    config_digest: str,
) -> str:
    """Render the ``repo://`` scheme ref naming one runnable repository script.

    ``repo://<installation>/<repository>@<commit>/<script>?cfg=<config hex>``
    -- audit records name both where the code came from and the exact manifest
    configuration it ran under. The ``cfg`` value is the bare hex of the
    config digest.
    """
    digest_hex = config_digest.removeprefix("sha256:")
    return (
        f"repo://{installation_id}/{repository_id}@{commit_sha}/{script_name}"
        f"?cfg={digest_hex}"
    )


_MANIFEST_REF = re.compile(
    r"\Arepo://(?P<installation_id>\d+)/(?P<repository_id>\d+)"
    r"@(?P<commit_sha>[0-9a-f]{40})/(?P<script_name>[a-z][a-z0-9_-]{0,63})"
    r"\?cfg=(?P<config_digest>[0-9a-f]{64})\Z"
)


def parse_repository_manifest_ref(ref: str) -> RepositoryManifestRefParts | None:
    """Parse a ``repo://`` manifest ref, or return ``None`` for anything else."""
    match = _MANIFEST_REF.match(ref)
    if match is None:
        return None
    return RepositoryManifestRefParts(
        installation_id=int(match["installation_id"]),
        repository_id=int(match["repository_id"]),
        commit_sha=match["commit_sha"],
        script_name=match["script_name"],
        config_digest="sha256:" + match["config_digest"],
    )


def build_repository_manifest(
    document: RepoManifestDocument,
    *,
    script_name: str,
    staged: StagedCheckout,
    repository_id: int,
    installation_id: int,
    config_digest: str,
    policy: RepoUnitPolicy,
) -> RepositoryUnitManifest:
    """Translate one declared script into an executable-unit manifest.

    Call only with a document that passed ``parse_manifest_document``,
    ``evaluate_policy``, and ``validate_staged_manifest`` against ``staged``.

    The sandbox layout (see the module docstring): the working directory is
    the document's working directory re-rooted under ``checkout/``, and the
    command names the entry relative to that working directory -- ``..``
    segments are fine, containment was validated at manifest validation.
    Resource limits inherit the policy ceilings wherever the script did not
    request a value, so an unstated request never means unbounded.

    Raises:
        ValueError: When ``script_name`` is not declared by the document.
    """
    script = document.scripts.get(script_name)
    if script is None:
        raise ValueError("script is not declared by the repository manifest")

    working_directory = (
        PurePosixPath(REPOSITORY_CHECKOUT_DIRNAME) / script.working_directory
    ).as_posix()
    entry_relative = posixpath.relpath(script.entry, start=script.working_directory)
    resources = script.resources
    limits = ResourceLimits(
        cpu_cores=resources.cpu_cores if resources.cpu_cores is not None else policy.max_cpu_cores,
        memory_mb=resources.memory_mb if resources.memory_mb is not None else policy.max_memory_mb,
        timeout_seconds=(
            resources.timeout_seconds
            if resources.timeout_seconds is not None
            else policy.max_timeout_seconds
        ),
        max_processes=(
            resources.max_processes
            if resources.max_processes is not None
            else policy.max_processes
        ),
        network_access=script.network.access is NetworkAccess.FULL,
    )
    output_mode = (
        OutputMode.EXIT_CODE_ONLY
        if script.output.mode is RepoOutputMode.EXIT_CODE_ONLY
        else OutputMode.JSON_STDOUT
    )
    return RepositoryUnitManifest(
        unit_id=f"repo:{repository_id}:{script_name}",
        version=1,
        artifact_source=RepositoryCheckoutArtifactSource(
            ref=staged.tree_digest,
            commit_sha=staged.commit_sha,
            config_digest=config_digest,
            repository_id=repository_id,
            installation_id=installation_id,
        ),
        run_config=RunConfig(
            command=["python3", "-I", entry_relative],
            working_directory=working_directory,
            environment=dict(script.environment),
        ),
        # A v1 "input: none" script simply never reads the piped stdin; the
        # payload still travels the same governed door as every other unit.
        input_mode=InputMode.JSON_STDIN,
        output_mode=output_mode,
        input_contract_ref=REPOSITORY_CONTRACT_REF,
        output_contract_ref=REPOSITORY_CONTRACT_REF,
        capability_requests=list(script.capabilities),
        resource_limits=limits,
        timeout_seconds=limits.timeout_seconds,
    )


def build_repository_binding(manifest: RepositoryUnitManifest) -> ExecutableUnitBinding:
    """Binding for a repository unit, ready for ``ExecutableUnitRunner.run_binding``.

    Mirrors the inline synthesis: freeform payload models on both sides and no
    Python handler -- repository units only ever run as sandboxed
    subprocesses. The environment allowlist is the sandbox builder's defaults
    plus the manifest's own environment keys, so a declared variable survives
    the sandbox environment filter and nothing else is added.
    """
    _, _, script_name = manifest.unit_id.partition(":")[2].partition(":")
    manifest_ref = build_repository_manifest_ref(
        installation_id=manifest.artifact_source.installation_id,
        repository_id=manifest.artifact_source.repository_id,
        commit_sha=manifest.artifact_source.commit_sha,
        script_name=script_name,
        config_digest=manifest.artifact_source.config_digest,
    )
    allowed_env_keys = tuple(
        dict.fromkeys((*_DEFAULT_ALLOWED_ENV_KEYS, *manifest.run_config.environment))
    )
    return ExecutableUnitBinding(
        manifest_ref=manifest_ref,
        manifest=manifest,
        input_model=FreeformPayload,
        output_model=FreeformPayload,
        allowed_env_keys=allowed_env_keys,
    )


@dataclass(frozen=True, slots=True)
class SmokeOutcome:
    """Verdict of the smoke assertions: pass, or the name of the failed check.

    ``failed_check`` is ``"exit_code"`` or ``"stdout_contains"`` -- the name
    of the assertion, never the observed output. Stdout from an
    author-supplied script must not ride a verdict into logs or audit rows.
    """

    passed: bool
    failed_check: str | None = None


def evaluate_smoke_assertions(
    spec: ScriptSpec, *, exit_code: int, stdout_text: str
) -> SmokeOutcome:
    """Judge a smoke run against the script's declared assertions.

    Without a ``smoke`` block the defaults apply: exit code ``0`` and no
    stdout check. ``files_exist`` is a staging-time check
    (:func:`validate_staged_manifest`), not re-evaluated here.
    """
    expected_exit = spec.smoke.exit_code if spec.smoke is not None else 0
    if exit_code != expected_exit:
        return SmokeOutcome(passed=False, failed_check="exit_code")
    needle = spec.smoke.stdout_contains if spec.smoke is not None else None
    if needle is not None and needle not in stdout_text:
        return SmokeOutcome(passed=False, failed_check="stdout_contains")
    return SmokeOutcome(passed=True)
