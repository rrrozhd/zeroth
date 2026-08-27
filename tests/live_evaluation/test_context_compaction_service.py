from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_context_checkpoint_service_leaves_no_writable_repo_residue(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    forbidden = (repo / "econ_plane.db", repo / ".zeroth")
    assert all(not path.exists() for path in forbidden), "test requires a clean repository root"

    external = tmp_path / "context-checkpoint"
    database = external / "state.sqlite3"
    port = _available_port()
    env = os.environ.copy()
    for name in (
        "ECP_DATABASE_URL",
        "ECP_CONNECTOR_SPOOL_ROOT",
        "ZEROTH_ARTIFACT_STORE__FILESYSTEM_BASE_DIR",
        "ZEROTH_DATABASE__SQLITE_PATH",
    ):
        env.pop(name, None)
    env["ZEROTH_CONTEXT_CHECKPOINT_API_KEY"] = "local-checkpoint-credential-for-test-only"
    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/context_compaction_checkpoint_service.py",
            "--database",
            str(database),
            "--external-root",
            str(external),
            "--port",
            str(port),
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"checkpoint service exited with {process.returncode}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("checkpoint service did not become healthy")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert all(not path.exists() for path in forbidden)
    assert database.exists()
    assert (external / "econ-plane.sqlite3").exists()
    assert (external / "artifacts").is_dir()
