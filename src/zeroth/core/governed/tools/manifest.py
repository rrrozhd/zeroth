"""ToolManifest: read-only serializable descriptor for Tool metadata.

Carries all Tool data fields except the callable. Enables Zeroth Studio
to display tool metadata and the policy engine to evaluate capabilities
without loading Python callables.

No reconstruction path exists (per D-09): ToolManifest is one-way extract only.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from zeroth.core.governed.tools.base import ExecutionPlacement


class ToolManifest(BaseModel):
    """Serializable read-only descriptor extracted from a Tool instance.

    Contains all metadata fields needed for display, policy evaluation,
    and capability checks without requiring a live Tool or its callable.
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    schema_fingerprint: str | None = None
    capabilities: list[str] = []
    side_effect: bool = False
    timeout_seconds: float | None = None
    requires_approval: bool = False
    tags: list[str] = []
    executor_type: str = "python"
    execution_placement: ExecutionPlacement = "local_only"
    remote_name: str | None = None
