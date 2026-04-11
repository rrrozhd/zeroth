"""Minimal PKG-06 acceptance fixture for zeroth-core.

This script is the canonical end-to-end smoke test for a clean-venv install
of ``zeroth-core``. Plan 28-03's release workflow installs the freshly-built
wheel into a scratch virtualenv and executes this file — if it runs, the
package is considered publishable.

Run:

    python examples/hello.py

Requires:

    ANTHROPIC_API_KEY   — if unset, the script prints a SKIP notice to stderr
                          and exits 0 (so forked-PR CI jobs without secrets
                          do not fail).

Phase 30 will wrap this file in a Getting Started tutorial; until then it
is deliberately tiny — an import-smoke plus one real LLM call — rather than
a feature tour.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "SKIP: set ANTHROPIC_API_KEY to run examples/hello.py against a real LLM",
            file=sys.stderr,
        )
        return 0

    # Import-smoke for PKG-06: prove the zeroth.core namespace actually loads
    # from the installed wheel. If this import fails in the release-workflow
    # clean-venv step, the wheel is broken.
    import zeroth.core  # noqa: F401

    # Phase 28 intentionally uses the ``litellm`` direct-call fallback described
    # in 28-02-PLAN §interfaces: the full orchestrator/graph builder requires
    # service bootstrap that does not belong in a 30-line example. ``litellm``
    # is a base dependency of zeroth-core, so importing it here does not require
    # any extras. Phase 30 will replace this with a proper graph walkthrough.
    from litellm import completion

    response = completion(
        model="anthropic/claude-3-haiku-20240307",
        messages=[
            {
                "role": "user",
                "content": "Say hello from zeroth-core in one short sentence.",
            }
        ],
    )

    # litellm returns an OpenAI-compatible response object.
    content = response["choices"][0]["message"]["content"]
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
