#!/usr/bin/env python3
"""Executable unit: format research findings.

Reads JSON from stdin, adds metadata, writes JSON to stdout.
No LLM call -- pure computation.
"""
import json
import sys
from datetime import datetime, timezone


def main():
    data = json.loads(sys.stdin.read())
    findings = data.get("findings", "")
    words = findings.split()
    formatted = (
        f"# Research Findings\n\n"
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"**Word count:** {len(words)}\n\n"
        f"---\n\n"
        f"{findings}"
    )
    json.dump({"formatted": formatted, "word_count": len(words)}, sys.stdout)


if __name__ == "__main__":
    main()
