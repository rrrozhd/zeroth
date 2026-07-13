from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


def _cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.output).resolve()
    if out.exists() and not args.force:
        print(f"Config already exists: {out} (use --force to overwrite)")
        return 1
    out.write_text(
        json.dumps(
            {
                "base_url": args.base_url,
                "capture_mode": "hierarchical",
                "enabled": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Initialized {out}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).resolve()
    script = root / "demo" / "live_poc_simulation.py"
    if not script.exists():
        print(f"Demo script not found: {script}")
        return 1
    cmd = [
        "python3",
        str(script),
        "--base-url",
        args.base_url,
        "--days",
        str(args.days),
        "--requests-per-day",
        str(args.requests_per_day),
    ]
    if args.strict:
        cmd.append("--strict")
    return subprocess.call(cmd)


def _latest_report(repo_root: Path) -> Path | None:
    output_dir = repo_root / "demo" / "output"
    if not output_dir.exists():
        return None
    reports = sorted(output_dir.glob("report_*.json"))
    return reports[-1] if reports else None


def _nested_get(d: dict[str, Any], path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _cmd_compute(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    report = Path(args.report).resolve() if args.report else _latest_report(repo_root)
    if report is None or not report.exists():
        print("No report found. Run `regulus demo` first or pass --report.")
        return 1

    data = json.loads(report.read_text(encoding="utf-8"))
    margin = _nested_get(data, "economic_summary.post_rebalance.net_margin_usd")
    aer = _nested_get(data, "economic_summary.post_rebalance.aer")
    ci = _nested_get(data, "confidence_summary")
    print(f"Report: {report}")
    print(f"Window: {args.last}")
    print(f"Net Margin: {margin}")
    print(f"AER: {aer}")
    print(f"Confidence Summary: {ci}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="regulus", description="Regulus OSS CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize local Regulus config")
    p_init.add_argument("--base-url", default="http://localhost:8000/v1")
    p_init.add_argument("--output", default="regulus.json")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=_cmd_init)

    p_demo = sub.add_parser("demo", help="Run live-like demo simulation")
    p_demo.add_argument("--repo-root", default=".")
    p_demo.add_argument("--base-url", default="http://localhost:8000/v1")
    p_demo.add_argument("--days", type=int, default=14)
    p_demo.add_argument("--requests-per-day", type=int, default=220)
    p_demo.add_argument("--strict", action="store_true")
    p_demo.set_defaults(func=_cmd_demo)

    p_compute = sub.add_parser("compute", help="Compute/report key metrics from latest report")
    p_compute.add_argument("--repo-root", default=".")
    p_compute.add_argument("--report", default=None)
    p_compute.add_argument("--last", default="7d")
    p_compute.set_defaults(func=_cmd_compute)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
