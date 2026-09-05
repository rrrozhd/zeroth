"""Measure and verify current compatibility independently of historical evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from importlib.metadata import distributions, version
from pathlib import Path
from typing import Any

from packaging.utils import canonicalize_name

from release.gates.identity import file_digest, head_commit
from release.langgraph.langgraph_benchmark import evaluate

ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_UPSTREAM = {
    "langchain": "1.3.14",
    "langgraph": "1.2.9",
    "langgraph-api": "0.11.1",
    "langgraph-sdk": "0.4.2",
}
PACKAGES = (
    *SUPPORTED_UPSTREAM,
    "zeroth-core",
    "langgraph-checkpoint-sqlite",
    "httpx",
    "h2",
    "websockets",
)


def source_identity(root: Path) -> dict[str, str]:
    """Hash the tracked source bytes, including edits, without generated output."""
    names = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"]).split(b"\0")
    digest = hashlib.sha256()
    for raw in sorted(name for name in names if name):
        path = root / os.fsdecode(raw)
        content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        digest.update(raw + b"\0" + hashlib.sha256(content).digest())
    return {
        "commit": head_commit(root),
        "tree_digest": "sha256:" + digest.hexdigest(),
        "lock_digest": file_digest(root / "uv.lock"),
    }


def measure(root: Path) -> dict[str, Any]:
    """Capture expected source and observed versions; make no test-pass claim."""
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    names = {canonicalize_name(item.metadata["Name"]) for item in distributions()} | set(PACKAGES)
    observed = {name: version(name) for name in sorted(names)}
    for name, expected in SUPPORTED_UPSTREAM.items():
        if observed[name] != expected:
            raise ValueError(f"unsupported upstream version: {name}")
    for name, installed in observed.items():
        locked = {
            p["version"]
            for p in lock["package"]
            if canonicalize_name(p["name"]) == canonicalize_name(name)
        }
        if installed not in locked:
            raise ValueError(f"installed version differs from lock: {name}")
    if observed["zeroth-core"] != project["project"]["version"]:
        raise ValueError("installed package release differs from project")
    return {
        "schema_version": 1,
        "release": observed["zeroth-core"],
        "supported_upstream": SUPPORTED_UPSTREAM,
        "observed_versions": observed,
        "dependency_declarations": {
            "core": project["project"]["dependencies"],
            "extras": {
                name: project["project"]["optional-dependencies"][name]
                for name in ("langgraph", "langgraph-gateway")
            },
            "conformance": project["dependency-groups"]["gateway-conformance"],
        },
        "source": source_identity(root),
    }


def verify(root: Path, snapshot: Path, identity: Path) -> dict[str, Any]:
    """Reject changed snapshot bytes, source, declared or installed versions."""
    expected = json.loads(snapshot.read_text())
    bound = json.loads(identity.read_text())
    if bound.get("compatibility") != file_digest(snapshot):
        raise ValueError("candidate compatibility digest mismatch")
    actual = measure(root)
    if actual != expected:
        raise ValueError("current compatibility differs from candidate snapshot")
    if (
        bound.get("commit") != actual["source"]["commit"]
        or bound.get("package", {}).get("version") != actual["release"]
    ):
        raise ValueError("candidate source or package identity mismatch")
    return actual


def check_conformance(path: Path) -> None:
    """Require actual test cases and reject skips, errors and failures."""
    tree = ET.parse(path)  # noqa: S314 - locally produced pytest artifact
    cases = tree.findall(".//testcase")
    if not cases or any(tree.findall(f".//{kind}") for kind in ("skipped", "failure", "error")):
        raise ValueError("conformance must execute without skips or failures")


def check_benchmark(path: Path, release: str) -> None:
    """Require current attribution and every frozen benchmark comparison."""
    report = json.loads(path.read_text())
    if report.get("passed") is not True or report.get("release") != release:
        raise ValueError("benchmark failed or release mismatched")
    if report.get("sample_count", 0) < 20 or report.get("injected_regression") is not False:
        raise ValueError("benchmark sample count or workload invalid")
    if not report.get("stream_ordering", {}).get("valid"):
        raise ValueError("benchmark stream ordering invalid")
    evaluation = evaluate(report["observed"])
    if not all(evaluation.values()) or report.get("evaluation") != evaluation:
        raise ValueError("benchmark frozen comparisons failed")


def main() -> None:
    """Produce a snapshot or verify it before and after release checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "verify", "results"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--benchmark", type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        if args.output is None:
            parser.error("snapshot requires --output")
        data = measure(args.root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
    else:
        if args.snapshot is None or args.identity is None:
            parser.error("verification requires --snapshot and --identity")
        data = verify(args.root, args.snapshot, args.identity)
        if args.command == "results":
            if args.junit is None or args.benchmark is None:
                parser.error("results requires --junit and --benchmark")
            check_conformance(args.junit)
            check_benchmark(args.benchmark, data["release"])
        print("current candidate compatibility verified")


if __name__ == "__main__":
    main()
