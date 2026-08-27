from __future__ import annotations

import subprocess

import pytest

from zeroth.check.tape.storage import RawRecordingStore, TapeStorageError

from .test_models import _payload
from zeroth.check.tape.models import RawRecordingV1


def test_raw_store_writes_atomically_and_reloads_verified_recording(tmp_path) -> None:
    store = RawRecordingStore(tmp_path / ".zeroth/check/recordings")
    raw = RawRecordingV1.seal(**_payload())
    path = store.write(raw)
    assert store.load(path) == raw
    with pytest.raises(TapeStorageError, match="exists"):
        store.write(raw)


def test_default_raw_recording_path_is_gitignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", ".zeroth/check/recordings/example.json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_curated_path_is_not_gitignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "checks/tapes/example.json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
