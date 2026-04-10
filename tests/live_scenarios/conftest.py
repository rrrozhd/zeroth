"""Apply the `live` marker to every test in tests/live_scenarios/.

These end-to-end scenarios bootstrap a full service and are excluded from
the default CI run via pytest's `-m 'not live'` filter.
"""

import pytest

pytestmark = pytest.mark.live


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    for item in items:
        if "live_scenarios" in str(item.fspath):
            item.add_marker(pytest.mark.live)
