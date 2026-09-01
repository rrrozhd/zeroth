"""SQLAlchemy URL compatibility for operator-provided database URLs."""

from __future__ import annotations


def sqlalchemy_database_url(database_url: str) -> str:
    """Select psycopg 3 when a provider emits a driver-neutral Postgres URL."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    return database_url


__all__ = ["sqlalchemy_database_url"]
