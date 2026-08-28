from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.configure_dev_artifact_identity import measured_identity, write_identity


class _Runner:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = iter(outputs)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=next(self.outputs), stderr="")


def test_measured_identity_requires_clean_git_and_immutable_values(tmp_path: Path) -> None:
    runner = _Runner(["", "1" * 40 + "\n", "sha256:" + "2" * 64 + "\n"])
    assert measured_identity(tmp_path, "pilot:latest", runner=runner) == (
        "1" * 40,
        "sha256:" + "2" * 64,
    )
    assert runner.calls[-1] == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "pilot:latest",
    ]


def test_measured_identity_refuses_a_dirty_checkout(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="clean tracked working tree"):
        measured_identity(tmp_path, "pilot:latest", runner=_Runner([" M source.py\n"]))


def test_write_identity_preserves_secrets_and_replaces_stale_identity(tmp_path: Path) -> None:
    target = tmp_path / "zeroth.env"
    target.write_text(
        "OPENAI_API_KEY=preserved\n"
        "ZEROTH_CERTIFICATION__SERVING_APP_COMMIT=" + "3" * 40 + "\n"
        "ZEROTH_CERTIFICATION__SERVING_IMAGE_DIGEST=sha256:" + "4" * 64 + "\n",
        encoding="utf-8",
    )
    write_identity(target, "1" * 40, "sha256:" + "2" * 64)
    assert target.read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=preserved\n"
        "ZEROTH_CERTIFICATION__SERVING_APP_COMMIT=" + "1" * 40 + "\n"
        "ZEROTH_CERTIFICATION__SERVING_IMAGE_DIGEST=sha256:" + "2" * 64 + "\n"
    )
    assert target.stat().st_mode & 0o777 == 0o600
