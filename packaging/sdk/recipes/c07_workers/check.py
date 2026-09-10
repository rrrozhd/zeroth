"""Export the frozen reference workload as JSON and run either C07 worker against a deployment."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def load_workload(path: str):
    spec = importlib.util.spec_from_file_location("reference_workload", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reference_workload"] = module
    spec.loader.exec_module(module)
    return module


def export(workload, target: Path, workflow: str | None = None) -> Path:
    """The workload the TypeScript worker reads: runs with their fixed timestamps."""
    runs = []
    for spec in workload.RUNS:
        runs.append({**spec, "recorded_at": workload.run_time(spec["id"]).isoformat()})
    target.write_text(
        json.dumps(
            {
                "workflow": workflow or workload.WORKFLOW,
                "versions": list(workload.VERSIONS),
                "runs": runs,
            },
            indent=2,
        )
        + "\n"
    )
    return target


def node_command(*extra: str) -> list[str]:
    return ["node", *extra, str(HERE / "worker.ts")]


def run_node(
    url: str,
    key: str,
    workload_json: Path,
    version: str,
    *,
    flags: tuple[str, ...] = (),
    retries: int = 3,
    timeout_ms: int = 5000,
) -> dict:
    result = subprocess.run(
        [
            *node_command(*flags),
            "--zeroth-url",
            url,
            "--zeroth-key",
            key,
            "--workload",
            str(workload_json),
            "--version",
            version,
            "--retries",
            str(retries),
            "--timeout-ms",
            str(timeout_ms),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    return {"returncode": result.returncode, "stderr": result.stderr[-2000:], **report}


def compare_node(url: str, key: str, workload_json: Path, baseline: str, candidate: str) -> dict:
    result = subprocess.run(
        [
            *node_command(),
            "--zeroth-url",
            url,
            "--zeroth-key",
            key,
            "--workload",
            str(workload_json),
            "--compare",
            baseline,
            candidate,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_python(
    url: str,
    key: str,
    workload_json: Path,
    version: str,
    *,
    retries: int = 3,
    env: dict | None = None,
) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "python_worker.py"),
            "--zeroth-url",
            url,
            "--zeroth-key",
            key,
            "--workload",
            str(workload_json),
            "--version",
            version,
            "--retries",
            str(retries),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
    return {"returncode": result.returncode, "stderr": result.stderr[-2000:], **report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True, help="path to the frozen workload.py")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--worker", choices=["python", "node"], default="python")
    args = parser.parse_args(argv)
    workload_json = export(load_workload(args.workload), HERE / "workload.generated.json")
    runner = run_python if args.worker == "python" else run_node
    report = runner(args.zeroth_url, args.zeroth_key, workload_json, args.version)
    print(json.dumps(report))
    return report.get("returncode", 1)


if __name__ == "__main__":
    raise SystemExit(main())
