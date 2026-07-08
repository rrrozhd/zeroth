"""Helpers for inline executable units — code authored in the Studio's code node.

An inline unit has no registry entry: its source lives in the graph node
itself, so its manifest and binding are synthesized on demand. Identity is
content-addressed (``sha256`` of the source), which means a published graph
pins the exact code it was validated with.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict

from zeroth.core.execution_units.models import (
    InlineSourceArtifactSource,
    InlineUnitManifest,
    InputMode,
    OutputMode,
    RunConfig,
)
from zeroth.core.execution_units.runner import ExecutableUnitBinding

# Inline source travels inside graph payload rows and diff output — cap it
# well below anything that would strain those paths.
INLINE_SOURCE_MAX_CHARS = 65_536

# How the sandboxed process is invoked; the runner materializes the source as
# this file in the sandbox working directory. -I = isolated mode (ignores
# PYTHON* env vars and the user site directory).
INLINE_ENTRY_FILENAME = "main.py"
INLINE_COMMAND = ("python3", "-I", INLINE_ENTRY_FILENAME)


class FreeformPayload(BaseModel):
    """Schema-less payload for inline units.

    Node-level contracts (``input_contract_ref``/``output_contract_ref``)
    remain the typed boundary; the unit itself accepts whatever the graph
    routes to it.
    """

    model_config = ConfigDict(extra="allow")


def inline_source_digest(source: str) -> str:
    """Content-addressed identity for authored source text."""
    return f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"


def build_inline_manifest(
    unit_id: str,
    source: str,
    *,
    timeout_seconds: int | None = None,
    input_contract_ref: str = "contract://inline-freeform",
    output_contract_ref: str = "contract://inline-freeform",
) -> InlineUnitManifest:
    """Synthesize the manifest for one code node's source."""
    return InlineUnitManifest(
        unit_id=unit_id,
        artifact_source=InlineSourceArtifactSource(
            ref=inline_source_digest(source),
            source=source,
        ),
        run_config=RunConfig(command=list(INLINE_COMMAND)),
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref=input_contract_ref,
        output_contract_ref=output_contract_ref,
        timeout_seconds=timeout_seconds,
    )


def build_inline_binding(
    node_id: str,
    source: str,
    *,
    timeout_seconds: int | None = None,
) -> ExecutableUnitBinding:
    """Binding for a code node, ready for ``ExecutableUnitRunner.run_binding``.

    The manifest ref embeds the node id and the source digest so audit
    records name both the step and the exact code that ran.
    """
    manifest = build_inline_manifest(node_id, source, timeout_seconds=timeout_seconds)
    return ExecutableUnitBinding(
        manifest_ref=f"inline://{node_id}/{manifest.artifact_source.ref}",
        manifest=manifest,
        input_model=FreeformPayload,
        output_model=FreeformPayload,
    )
