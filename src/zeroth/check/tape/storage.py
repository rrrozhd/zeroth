"""Filesystem boundaries for raw and curated Check artifacts."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from pydantic import ValidationError

from zeroth.check.tape.models import RawRecordingV1


class TapeStorageError(RuntimeError):
    """An artifact could not be safely stored or loaded."""


def atomic_write(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise TapeStorageError(f"artifact already exists: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class RawRecordingStore:
    """Raw-only store whose default directory is intentionally gitignored."""

    def __init__(self, root: str | Path = ".zeroth/check/recordings") -> None:
        self.root = Path(root).resolve()

    def write(self, recording: RawRecordingV1, *, overwrite: bool = False) -> Path:
        stem = re.sub(
            r"[^A-Za-z0-9_.-]+", "-", f"{recording.case_id}-{recording.scenario_run_id}"
        ).strip("-.")
        if not stem:
            raise TapeStorageError("recording identifiers do not produce a safe filename")
        path = self.root / f"{stem}.json"
        atomic_write(path, recording.canonical_bytes(), overwrite=overwrite)
        return path

    def load(self, path: str | Path) -> RawRecordingV1:
        try:
            return RawRecordingV1.model_validate_json(Path(path).read_bytes())
        except (OSError, ValidationError) as exc:
            raise TapeStorageError("invalid raw recording") from exc
