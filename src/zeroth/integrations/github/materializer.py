"""Copy a staged checkout into a runner destination, belt-and-braces safe.

The staged root already passed both verification phases, but the copy still
refuses symlinks outright (belt: a swap between verification and copy must not
smuggle one through) and preserves executable bits so manifest entrypoints
stay runnable. The actual runner seam is wired by a later phase; this local
materializer is the reference implementation the tests exercise.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode


class LocalCheckoutMaterializer:
    """Copy staged trees on the local filesystem, refusing hostile shapes."""

    def materialize(self, staged_root: Path, destination: Path) -> None:
        """Copy ``staged_root`` into ``destination`` preserving exec bits.

        Raises:
            CheckoutError: With ``tree_symlink`` when any entry is a symlink,
                or ``tree_traversal`` when an entry is neither a regular file
                nor a directory.
        """
        staged_root = Path(staged_root)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for current_dir, dir_names, file_names in os.walk(staged_root, followlinks=False):
            current = Path(current_dir)
            relative = current.relative_to(staged_root)
            target_dir = destination / relative
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in dir_names:
                if (current / name).is_symlink():
                    raise CheckoutError(
                        CheckoutFailureCode.TREE_SYMLINK,
                        "staged tree contains a symlink",
                    )
            for name in file_names:
                source = current / name
                mode = source.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise CheckoutError(
                        CheckoutFailureCode.TREE_SYMLINK,
                        "staged tree contains a symlink",
                    )
                if not stat.S_ISREG(mode):
                    raise CheckoutError(
                        CheckoutFailureCode.TREE_TRAVERSAL,
                        "staged tree contains a non-regular file",
                    )
                target = target_dir / name
                shutil.copyfile(source, target)
                target.chmod(0o755 if stat.S_IMODE(mode) & 0o111 else 0o644)


__all__ = ["LocalCheckoutMaterializer"]
