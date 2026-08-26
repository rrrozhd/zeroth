"""Filesystem semantics for a repository manifest against a staged checkout.

The other half of the ZER-37 pipeline:
:func:`zeroth.contracts.repo_manifest.parse_manifest_document` is pure and has
already bounded every path *syntactically*; this module answers the questions
only a real checkout can -- does the entry resolve inside the staged root, is
it a regular file, is the working directory a directory, are the smoke files
present. Deliberately minimal and forward-extensible: translating a validated
manifest into an execution-unit manifest is a later phase, and nothing here
should grow toward it.

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
field.
"""

from __future__ import annotations

import stat
from pathlib import Path

from zeroth.contracts.repo_manifest import (
    RepoManifestDocument,
    RepoManifestIssue,
    RepoManifestSeverity,
    RepoManifestValidationCode,
    RepoManifestValidationReport,
)
from zeroth.platform.primitives import (
    DestinationNotADirectoryError,
    OutboundDestinationError,
    confine_directory,
    confine_path,
)

__all__ = ["validate_staged_manifest"]


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
