"""Skip tests whose external release inputs have not been restored.

The frozen measurements and protocols pinned by ``release/inputs-v1.json`` are kept
out of Git and must never reach origin, so a clean checkout -- CI included -- does
not have them. A test that reads one is skipped there, naming the missing files and
the restore command; wherever the archive has been restored, the test runs. Only
paths the manifest declares can be skipped, so a missing tracked file still fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_INPUTS = frozenset(
    json.loads((ROOT / "release/inputs-v1.json").read_text(encoding="utf-8"))["files"]
)


def requires_release_inputs(*paths: str) -> pytest.MarkDecorator:
    """Skip unless every named external release input exists in this checkout."""
    undeclared = sorted(set(paths) - EXTERNAL_INPUTS)
    if undeclared:
        raise ValueError(f"not external release inputs: {undeclared}")
    missing = [path for path in paths if not (ROOT / path).is_file()]
    return pytest.mark.skipif(
        bool(missing),
        reason=(
            f"external release inputs not restored: {', '.join(missing)} "
            "(scripts/restore_release_inputs.py)"
        ),
    )
