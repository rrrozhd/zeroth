"""Submit audit queries to a running research-audit deployment."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import httpx


def _poll_run(
    client: httpx.Client,
    base_url: str,
    run_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"{base_url}/runs/{run_id}").json()
        if payload["status"] in {
            "succeeded",
            "failed",
            "terminated_by_policy",
            "terminated_by_loop_guard",
            "paused_for_approval",
        }:
            return payload
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for run {run_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--timeout", default=30.0, type=float)
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve paused approval gates.",
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help="Audit questions to submit. If omitted, two built-in smoke queries are used.",
    )
    args = parser.parse_args()

    if args.queries:
        prompts = [
            {"question": query, "use_web": False, "force_approval": False}
            for query in args.queries
        ]
    else:
        prompts = [
            {
                "question": (
                    "Find likely bugs in Zeroth service bootstrap around thread "
                    "handling and policy wiring."
                ),
                "use_web": False,
                "force_approval": False,
            },
            {
                "question": (
                    "Find likely bugs in tool attachment execution, approval "
                    "resume, and audit persistence."
                ),
                "use_web": False,
                "force_approval": True,
            },
        ]

    with httpx.Client(timeout=args.timeout) as client:
        for prompt in prompts:
            response = client.post(
                f"{args.base_url}/runs",
                json={"input_payload": prompt},
            )
            response.raise_for_status()
            created = response.json()
            run_id = created["run_id"]
            final = _poll_run(client, args.base_url, run_id, timeout=args.timeout)

            if final["status"] == "paused_for_approval":
                if not args.auto_approve:
                    print(json.dumps(final, indent=2))
                    raise RuntimeError(
                        f"run {run_id} paused for approval; rerun with --auto-approve to continue"
                    )
                approval_id = final["approval_paused_state"]["approval_id"]
                resolved = client.post(
                    f"{args.base_url}/deployments/{created['deployment_ref']}/approvals/{approval_id}/resolve",
                    json={"decision": "approve", "approver": "run_queries"},
                )
                resolved.raise_for_status()
                final = _poll_run(client, args.base_url, run_id, timeout=args.timeout)

            print(json.dumps({"prompt": prompt, "result": final}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
