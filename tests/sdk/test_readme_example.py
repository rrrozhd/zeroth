"""Execute the public quickstart against an isolated HTTP transport."""

import json
from pathlib import Path
import re

import httpx
import pytest


def test_readme_sdk_example_records_execution_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(201, json={"id": "example-execution"})

    transport = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: transport)
    monkeypatch.setenv("ZEROTH_API_KEY", "example-project-key")
    monkeypatch.setenv("ZEROTH_BASE_URL", "https://example.invalid/regulus")
    readme = Path(__file__).resolve().parents[2] / "README.md"
    example = re.search(r"```python\n(.*?)\n```", readme.read_text(), re.S)
    assert example is not None
    exec(compile(example.group(1), "README quickstart", "exec"), {})
    assert len(requests) == 1
    assert str(requests[0].url) == "https://example.invalid/regulus/v1/executions"
    assert requests[0].headers["Authorization"] == "Bearer example-project-key"
    assert json.loads(requests[0].content)["cost_usd"] == "0.031"
    assert transport.is_closed
