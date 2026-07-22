"""Identifier primitives for backend code."""

from uuid import uuid4


def new_uuid() -> str:
    """Return a new UUID as its canonical string representation."""
    return str(uuid4())
