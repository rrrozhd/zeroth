from __future__ import annotations

import json

import pytest

from zeroth.check.tape.migrations import MIGRATIONS, UnsupportedTapeVersionError, load_tape
from zeroth.check.tape.models import RawRecordingV1, TapeV1

from .test_models import _payload


def test_loader_accepts_only_verified_tape_v1(tmp_path) -> None:
    raw = RawRecordingV1.seal(**_payload())
    tape = TapeV1.seal_from_raw(
        raw,
        scrubber_version="scrubber.v1",
        secret_rules_version="secret_rules.v1",
        reviewer_id="reviewer",
        approved_at="2026-08-19T18:00:00Z",
        identity_changed_by_scrubbing=False,
    )
    path = tmp_path / "tape.json"
    path.write_bytes(tape.canonical_bytes())
    assert load_tape(path) == tape


@pytest.mark.parametrize("version", ["raw_recording.v1", "tape.v0", "tape.v2"])
def test_unknown_and_raw_versions_are_invalid(tmp_path, version: str) -> None:
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps({"schema_version": version}))
    with pytest.raises(UnsupportedTapeVersionError):
        load_tape(path)


def test_v1_has_no_fabricated_predecessor_migration() -> None:
    assert MIGRATIONS == {}
