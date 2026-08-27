"""Pinned capacity identity for load and recovery evidence."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Any


def _cgroup_value(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def runtime_environment() -> dict[str, str | int]:
    """Describe the pinned capacity boundary used by the release gate."""
    cpu_limit = os.cpu_count() or 1
    cpu_max = _cgroup_value("/sys/fs/cgroup/cpu.max").split()
    if len(cpu_max) == 2 and cpu_max[0] != "max":
        cpu_limit = max(1, int(cpu_max[0]) // int(cpu_max[1]))
    memory_max = _cgroup_value("/sys/fs/cgroup/memory.max")
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_limit": cpu_limit,
        "memory_limit_bytes": 0 if memory_max in {"", "max"} else int(memory_max),
        "runtime_image": os.environ.get("ZEROTH_LOAD_RUNTIME_IMAGE", ""),
        "postgres": os.environ.get("ZEROTH_LOAD_POSTGRES_VERSION", ""),
        "redis": os.environ.get("ZEROTH_LOAD_REDIS_VERSION", ""),
    }


def runtime_service_instances(environment: dict[str, str | int]) -> dict[str, dict[str, str]]:
    """Bind the report to the service processes used for this measurement."""
    instances = {}
    for service in ("postgres", "redis"):
        prefix = f"ZEROTH_LOAD_{service.upper()}"
        instances[service] = {
            "instance_id": os.environ.get(f"{prefix}_INSTANCE_ID", ""),
            "started_at": os.environ.get(f"{prefix}_STARTED_AT", ""),
            "image": str(environment[service]),
        }
    return instances


def observation_digest(rows: Any) -> str:
    """Hash canonical raw observations independently of report summaries."""
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()
