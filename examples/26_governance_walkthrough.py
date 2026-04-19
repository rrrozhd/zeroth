"""26 — Governance walkthrough: run the three focused governance examples in sequence.

What this shows
---------------
Umbrella runner for 20, 21, 22, 24 — the approval gate, the policy
block, the budget cap, and the audit query. Each focused file is
self-contained; this one just sequences them so a first-time reader
can see the whole governance surface without eyeballing four terminals.

If you want to study one scenario in isolation, run its file directly.
If you want a "show me everything at once" entry point, run this one.

Run
---
    uv run python examples/26_governance_walkthrough.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import importlib.util
import sys
from pathlib import Path

FOCUSED_EXAMPLES: list[tuple[str, str]] = [
    ("Approval gate", "20_approval_gate.py"),
    ("Policy block", "21_policy_block.py"),
    ("Budget cap", "22_budget_cap.py"),
    ("Audit query", "24_audit_query.py"),
]


def _load_module(relative_path: str):
    path = Path(__file__).parent / relative_path
    spec = importlib.util.spec_from_file_location(
        f"examples._walkthrough_{path.stem}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    for label, filename in FOCUSED_EXAMPLES:
        banner = f"── {label} ({filename}) "
        print(banner + "─" * max(1, 72 - len(banner)))
        module = _load_module(filename)
        main_fn = getattr(module, "main_async", None) or module.main
        result = main_fn()
        if asyncio.iscoroutine(result):
            await result
        print()
    print("all governance scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
