"""Capture the deterministic HTTP operation projection bundled by langgraph-api."""

from __future__ import annotations

import argparse
import json
from importlib import metadata
from pathlib import Path

HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})


def capture_operations(distribution_name: str = "langgraph-api") -> dict[str, object]:
    """Return package version and sorted method/path/operationId rows."""
    package_files = metadata.files(distribution_name)
    if package_files is None:
        raise RuntimeError(f"distribution has no installed files: {distribution_name}")
    candidates = sorted(path for path in package_files if path.name == "openapi.json")
    if len(candidates) != 1:
        rendered = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(f"expected one bundled openapi.json, found: {rendered}")

    schema = json.loads(candidates[0].locate().read_text(encoding="utf-8"))
    operations = sorted(
        [method.upper(), path, operation.get("operationId")]
        for path, path_item in schema.get("paths", {}).items()
        for method, operation in path_item.items()
        if method in HTTP_METHODS and isinstance(operation, dict)
    )
    return {
        "package_version": metadata.version(distribution_name),
        "operations": operations,
    }


def main() -> None:
    """Write the projection as stable, human-readable JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.dumps(capture_operations(), indent=2, sort_keys=True) + "\n"
    args.output.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()
