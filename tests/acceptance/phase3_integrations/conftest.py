"""Phase 3 conformance harness: the sold SDK source against the real economic routes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

SDK_SOURCE = Path(__file__).parents[3] / "packaging" / "sdk" / "src"
sys.path.insert(0, str(SDK_SOURCE))

from tests.acceptance.phase3_integrations import server  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    from zeroth.econ.plane.database import Base
    from zeroth.econ.plane.performance import models as performance_models  # noqa: F401

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'phase3.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def origin(engine):
    with server.serve(server.build_app(engine)) as url:
        yield url
