from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "release/langgraph/harness.py"
MANIFEST = ROOT / "release/langgraph/release-manifest.json"


def _validate(manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, HARNESS, "validate", "--manifest", manifest],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_manifest_is_complete_and_fails_closed(tmp_path: Path) -> None:
    assert _validate(MANIFEST).returncode == 0
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest["evidence"]) == {"compatibility", "security", "performance", "tests"}

    for category in manifest["evidence"]:
        broken = json.loads(MANIFEST.read_text(encoding="utf-8"))
        del broken["evidence"][category]
        path = tmp_path / f"missing-{category}.json"
        path.write_text(json.dumps(broken), encoding="utf-8")
        result = _validate(path)
        assert result.returncode != 0
        assert category in result.stderr
