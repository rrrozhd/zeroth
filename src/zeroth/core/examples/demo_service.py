"""Legacy import path for the shipped demo seeder.

It now lives in :mod:`zeroth.service.demo`; this module republishes exactly the
names it published before ZER-25 relocated it. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from __future__ import annotations

from zeroth.service.demo import (
    DEFAULT_DEMO_MODEL,
    DEMO_GRAPH_ID,
    DEMO_INPUT_CONTRACT,
    DEMO_OUTPUT_CONTRACT,
    DemoAnswer,
    DemoQuestion,
    build_hello_graph,
    seed_demo,
)

__all__ = [
    "DEFAULT_DEMO_MODEL",
    "DEMO_GRAPH_ID",
    "DEMO_INPUT_CONTRACT",
    "DEMO_OUTPUT_CONTRACT",
    "DemoAnswer",
    "DemoQuestion",
    "build_hello_graph",
    "seed_demo",
]
