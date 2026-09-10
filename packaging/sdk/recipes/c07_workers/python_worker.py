"""C07 - explicit economic events from a Python worker on the zeroth-sdk client.

Usage: python_worker.py --zeroth-url URL --zeroth-key KEY --workload workload.json --version v1

The worker delivers through ``Recorder`` without raising on delivery failures,
then replays ``recorder.lost`` a bounded number of times: identities are stable,
so a replayed event that had landed is acknowledged as a duplicate, never stored
twice. Everything pending is delivered before the process exits.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal

from zeroth.instrumentation import Recorder, Usage
from zeroth.sdk import ZerothClient


def replay(recorder: Recorder, workload: dict, version: str) -> None:
    for run_spec in workload["runs"]:
        at = datetime.fromisoformat(run_spec["recorded_at"])
        run = recorder.run(workload["workflow"], version, run_spec["id"])
        for call in run_spec["calls"]:
            if call["kind"] == "tool":
                run.tool_charge(
                    call["step"],
                    tool=call["tool"],
                    attempt=call["attempt"],
                    cost_usd=Decimal(call["cost_usd"][version]),
                    recorded_at=at,
                )
                continue
            tokens = call["usage"][version]
            run.charge(
                call["step"],
                attempt=call["attempt"],
                model=call["model"],
                provider=call["provider"],
                error=call.get("error"),
                recorded_at=at,
                usage=None if tokens is None else Usage(tokens[0], tokens[1], tokens[2]),
            )
        run.summary(run_spec["terminal"], recorded_at=at)
        if run_spec["outcome"]["delayed"]:
            run.outcome(None, maturity="provisional", occurred_at=at)
            at = at + timedelta(hours=1)
        run.outcome(run_spec["outcome"]["accepted"], occurred_at=at)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroth-url", required=True)
    parser.add_argument("--zeroth-key", required=True)
    parser.add_argument("--workload", required=True)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args(argv)
    with open(args.workload, encoding="utf-8") as handle:
        workload = json.load(handle)
    recorder = Recorder(
        ZerothClient(api_key=args.zeroth_key, base_url=args.zeroth_url), raise_on_error=False
    )
    replay(recorder, workload, args.version)
    attempts = 1
    while recorder.lost and attempts <= args.retries:
        attempts += 1
        pending, recorder.lost = list(recorder.lost), []
        for event, _ in pending:
            recorder.deliver(event)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "version": args.version,
                "lost": [type(e).__name__ for _, e in recorder.lost],
                "rounds": attempts,
            }
        )
    )
    return 0 if not recorder.lost else 1


if __name__ == "__main__":
    raise SystemExit(main())
