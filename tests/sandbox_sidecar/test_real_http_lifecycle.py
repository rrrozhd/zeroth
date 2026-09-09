"""Actual HTTP and Docker evidence for the sidecar release gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_real_http_cancellation_cleanup_and_client_reuse(tmp_path) -> None:
    if not os.environ.get("ZEROTH_TEST_DOCKER_IMAGE"):
        pytest.skip("set ZEROTH_TEST_DOCKER_IMAGE for real HTTP sidecar coverage")
    script = Path(__file__).with_name("_real_http_lifecycle_probe.py")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)], capture_output=True, text=True, timeout=90
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads((tmp_path / "result.json").read_text())
    assert evidence["started_container_ids"]
    assert evidence["caller_outcome"] == "cancelled"
    assert evidence["cancellation_seconds"] < 5
    assert not evidence["workspace_exists_after_return"]
    assert not evidence["remaining_containers"]
    assert not evidence["network_exists"]
    assert not evidence["volume_exists"]
    assert evidence["status_http"] == 200
    assert evidence["server_status"]["status"] == "cancelled"
    assert evidence["reused_client"] == {"returncode": 7, "stdout": "http-stdin-roundtrip\n"}
