"""Transport failures in the gateway client reach an operator (ZER-48 / A07-9).

``_post`` collapses every transport failure, unexpected status and JSON decode
error into a bare ``LangGraphGatewayError``.  Raising opaquely is deliberate —
the caller must not learn the gateway's transport detail.  Discarding the cause
entirely is not: the heartbeat path suppresses the error with no log at all, so
a gateway that has been unreachable for hours looks identical to one that is
healthy.

These tests pin the split: the *log* carries the cause, the *exception* does not.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from zeroth.integrations.langgraph._gateway_client import (
    LangGraphGatewayClient,
    LangGraphGatewayError,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
)

_LOGGER_NAME = "zeroth.integrations.langgraph._gateway_client"


def _client(handler) -> LangGraphGatewayClient:  # noqa: ANN001
    return LangGraphGatewayClient(
        "https://gateway.example.com",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        policy_version="policy-v1",
        graph_version="graph-v1",
        inventory=ToolInventory(
            entries=(
                ToolInventoryEntry(
                    identity=ToolIdentity("lookup", "sha256:tool"),
                    side_effect=SideEffectClass.READ_ONLY,
                ),
            )
        ),
        heartbeat_interval_seconds=None,
        transport=httpx.MockTransport(handler),
    )


class TestPostFailureIsLogged:
    def test_transport_error_is_logged_with_its_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

        client = _client(handler)

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
            pytest.raises(LangGraphGatewayError),
        ):
            client._post("heartbeat", {}, expected_status=204)

        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert records, "a gateway transport failure produced no log record"
        assert records[0].exc_info is not None, "the log record discarded the cause"

    def test_unexpected_status_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        client = _client(lambda request: httpx.Response(503))

        with (
            caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
            pytest.raises(LangGraphGatewayError),
        ):
            client._post("heartbeat", {}, expected_status=204)

        records = [r for r in caplog.records if r.name == _LOGGER_NAME]
        assert records, "an unexpected gateway status produced no log record"
        assert "503" in records[0].getMessage()

    def test_raised_error_still_carries_no_transport_detail(self) -> None:
        """Logging the cause must not start leaking it to the caller."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dsn=postgres://secret@db:5432", request=request)

        client = _client(handler)

        with pytest.raises(LangGraphGatewayError) as excinfo:
            client._post("heartbeat", {}, expected_status=204)

        assert "secret" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
