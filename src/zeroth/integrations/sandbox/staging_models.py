"""Response schema for the sidecar workspace-staging endpoint (ZER-37).

A separate module on purpose: ``models.py`` is a schema-bearing module whose
class inventory feeds the frozen protected-surface fixture, so the new
response model lives here where the canonical module surfaces stay untouched.
"""

from __future__ import annotations

from pydantic import BaseModel


class SidecarWorkspaceUploadResponse(BaseModel):
    """What the sidecar accepted when a workspace archive was staged."""

    workspace_id: str
    raw_bytes: int
    member_count: int
    total_file_bytes: int


__all__ = ["SidecarWorkspaceUploadResponse"]
