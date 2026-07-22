from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.econ.budget import BudgetCheckResult
from zeroth.core.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole

# Import the service package through its bootstrap path before importing the proxy;
# the package initializer itself imports bootstrap and otherwise creates a standalone
# subprocess-only circular import through ``proxy -> service.auth``.
import zeroth.core.service.bootstrap as _service_bootstrap
from zeroth.core.langgraph_gateway.compatibility import CompatibilityResult
from zeroth.core.langgraph_gateway.context import ReservedContextCodec
from zeroth.core.langgraph_gateway.models import CompatibilityStatus
from zeroth.core.langgraph_gateway.proxy import GatewayProxy
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport
from zeroth.core.policy.models import RunAdmissionResult
from zeroth.core.secrets.provider import EnvSecretProvider
from zeroth.core.signing import EnvHmacSigner


EXPECTED_GOVERNANCE_ADDITIONS = (
    "response.header.x-correlation-id",
    "response.header.x-zeroth-governance-level",
    "forwarded.config.configurable._zeroth",
    "audit.langgraph.gateway",
)

_GENERATED_FIELDS = {
    "assistant_id",
    "checkpoint_id",
    "created_at",
    "event_id",
    "run_id",
    "thread_id",
    "updated_at",
}
_ADDITION_HEADERS = {"x-correlation-id", "x-zeroth-governance-level"}
_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CapturedExchange:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    raw_chunks: tuple[bytes, ...]
    final_json: Any
    state: Any
    interrupts: tuple[Any, ...]
    resume_values: tuple[Any, ...]
    tool_sequence: tuple[Any, ...]
    errors: tuple[Any, ...]
    cancellation_outcome: Any
    terminal_state: Any
    forwarded_context_present: bool = False
    audit_event_present: bool = False


@dataclass(frozen=True, slots=True)
class DifferentialReport:
    semantic_divergences: list[str]
    expected_governance_additions: list[str]

    def write_human_report(self, path: Path) -> None:
        path.write_text(
            "LangGraph gateway differential report\n"
            f"semantic divergences: {self.semantic_divergences or ['none']}\n"
            f"expected governance additions: {self.expected_governance_additions or ['none']}\n",
            encoding="utf-8",
        )


def _normalize_generated(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key in _GENERATED_FIELDS:
                normalized[key] = "<generated>"
            elif key == "__interrupt__" and isinstance(item, list):
                normalized[key] = [
                    {
                        nested_key: "<generated>"
                        if nested_key == "id"
                        else _normalize_generated(nested_value)
                        for nested_key, nested_value in interrupt_value.items()
                    }
                    if isinstance(interrupt_value, Mapping)
                    else _normalize_generated(interrupt_value)
                    for interrupt_value in item
                ]
            else:
                normalized[key] = _normalize_generated(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_generated(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_generated(item) for item in value)
    return value


def _semantic_projection(capture: CapturedExchange, *, proxied: bool) -> dict[str, Any]:
    headers = tuple(
        (
            name.lower(),
            "<generated>" if name.lower() == "date" else _UUID_PATTERN.sub("<generated>", value),
        )
        for name, value in capture.headers
        if not (proxied and name.lower() in _ADDITION_HEADERS)
    )
    return {
        "status_code": capture.status_code,
        "headers": headers,
        "raw_chunks": capture.raw_chunks,
        "final_json": _normalize_generated(capture.final_json),
        "state": _normalize_generated(capture.state),
        "interrupts": _normalize_generated(capture.interrupts),
        "resume_values": _normalize_generated(capture.resume_values),
        "tool_sequence": _normalize_generated(capture.tool_sequence),
        "errors": _normalize_generated(capture.errors),
        "cancellation_outcome": capture.cancellation_outcome,
        "terminal_state": capture.terminal_state,
    }


def compare_exchanges(direct: CapturedExchange, proxied: CapturedExchange) -> DifferentialReport:
    direct_projection = _semantic_projection(direct, proxied=False)
    proxied_projection = _semantic_projection(proxied, proxied=True)
    divergences = [
        field
        for field in direct_projection
        if direct_projection[field] != proxied_projection[field]
    ]
    proxied_headers = {name.lower() for name, _ in proxied.headers}
    additions: list[str] = []
    if "x-correlation-id" in proxied_headers:
        additions.append("response.header.x-correlation-id")
    if "x-zeroth-governance-level" in proxied_headers:
        additions.append("response.header.x-zeroth-governance-level")
    if proxied.forwarded_context_present:
        additions.append("forwarded.config.configurable._zeroth")
    if proxied.audit_event_present:
        additions.append("audit.langgraph.gateway")
    return DifferentialReport(divergences, additions)


class _AllowPolicy:
    def evaluate_run_admission(self, request: object) -> RunAdmissionResult:
        return RunAdmissionResult(allowed=True, policy_version="sha256:conformance")


class _AllowBudget:
    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        return BudgetCheckResult(allowed=True, spend_usd=0, cap_usd=100)


class _RecordingSink:
    async def emit(self, event: object) -> None:
        return None


def _principal(_: Request) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject="conformance-user",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.OPERATOR],
        tenant_id="conformance-tenant",
    )


def create_gateway_app(
    upstream_url: str, *, event_sink: Any | None = None
) -> tuple[Starlette, HTTPGatewayTransport]:
    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url=upstream_url,
        upstream_audience="agent-server:conformance",
        deployment_ref="conformance-deployment",
    )
    transport = HTTPGatewayTransport(settings, EnvSecretProvider())
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(
            EnvHmacSigner(key_id="conformance", keys={"conformance": b"fixture-key"})
        ),
        policy_guard=_AllowPolicy(),
        budget_checker=_AllowBudget(),
        compatibility=CompatibilityResult(
            tested_langgraph_versions=("1.2.9",),
            tested_agent_server_versions=("0.11.1",),
            detected_agent_server_version="0.11.1",
            openapi_fingerprint="sha256:conformance",
            status=CompatibilityStatus.SUPPORTED,
        ),
        event_sink=event_sink or _RecordingSink(),
        principal_resolver=_principal,
    )

    async def route(request: Request) -> Response:
        return await proxy.handle_http(request)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await transport.aclose()

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                route,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ],
        lifespan=lifespan,
    )
    return app, transport


def run_gateway(upstream_url: str, port: int) -> None:
    app, _ = create_gateway_app(upstream_url)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
        date_header=False,
        server_header=False,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    with httpx.Client(timeout=0.25) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"server exited during startup with code {process.returncode}")
            try:
                if client.get(f"{url}/ok").status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.05)
    raise TimeoutError(f"server did not become ready: {url}")


class ConformanceServers:
    def __init__(self) -> None:
        self.direct_url = ""
        self.gateway_url = ""
        self._stack = ExitStack()
        self._processes: list[subprocess.Popen[bytes]] = []

    @staticmethod
    def client_configuration(url: str, api_key: str) -> dict[str, str]:
        return {"url": url, "api_key": api_key}

    def __enter__(self) -> ConformanceServers:
        repository_root = Path(__file__).parents[3]
        environment = os.environ.copy()
        environment.update(
            {
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
                "LANGGRAPH_CLOUD_LICENSE_KEY": "",
                "PYTHONPATH": os.pathsep.join(
                    filter(None, (str(repository_root), environment.get("PYTHONPATH")))
                ),
            }
        )
        direct_port = _free_port()
        direct_dir = self._stack.enter_context(
            tempfile.TemporaryDirectory(prefix="zeroth-direct-agent-")
        )
        direct_script = (
            "from langgraph_api.cli import run_server; "
            f"run_server(host='127.0.0.1', port={direct_port}, reload=False, "
            "graphs={'conformance':'tests.langgraph_gateway.conformance.graph:graph'}, "
            "disable_persistence=True, open_browser=False, server_level='ERROR')"
        )
        direct = subprocess.Popen(
            [sys.executable, "-c", direct_script],
            cwd=direct_dir,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes.append(direct)
        self.direct_url = f"http://127.0.0.1:{direct_port}"
        _wait_ready(self.direct_url, direct)

        gateway_port = _free_port()
        gateway_dir = self._stack.enter_context(
            tempfile.TemporaryDirectory(prefix="zeroth-conformance-gateway-")
        )
        gateway_script = (
            "from tests.langgraph_gateway.conformance.harness import run_gateway; "
            f"run_gateway({self.direct_url!r}, {gateway_port})"
        )
        gateway = subprocess.Popen(
            [sys.executable, "-c", gateway_script],
            cwd=gateway_dir,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes.append(gateway)
        self.gateway_url = f"http://127.0.0.1:{gateway_port}"
        _wait_ready(self.gateway_url, gateway)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for process in reversed(self._processes):
            process.terminate()
        for process in reversed(self._processes):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._stack.close()


def capture_response(response: httpx.Response, *, state: Any = None) -> CapturedExchange:
    content_type = response.headers.get("content-type", "")
    final_json = response.json() if content_type.startswith("application/json") else None
    return CapturedExchange(
        status_code=response.status_code,
        headers=tuple(response.headers.multi_items()),
        raw_chunks=(response.content,),
        final_json=final_json,
        state=state,
        interrupts=(),
        resume_values=(),
        tool_sequence=tuple((final_json or {}).get("tool_sequence", ()))
        if isinstance(final_json, dict)
        else (),
        errors=() if response.is_success else (final_json or response.text,),
        cancellation_outcome=None,
        terminal_state="success" if response.is_success else "error",
        forwarded_context_present="x-correlation-id" in response.headers,
        audit_event_present="x-correlation-id" in response.headers,
    )
