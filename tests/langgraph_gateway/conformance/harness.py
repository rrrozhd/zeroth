from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# Import the service package through its bootstrap path before importing the proxy;
# the package initializer itself imports bootstrap and otherwise creates a standalone
# subprocess-only circular import through ``proxy -> service.auth``.
import zeroth.core.service.bootstrap as _service_bootstrap  # noqa: F401 - see above
from zeroth.contracts.langgraph_gateway.models import (
    CompatibilityStatus,
    GovernanceLevel,
    RunCapabilityEvidence,
)
from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.econ.budget import BudgetCheckResult
from zeroth.core.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.core.policy.models import RunAdmissionResult
from zeroth.core.secrets.provider import EnvSecretProvider
from zeroth.core.signing import EnvHmacSigner
from zeroth.governance.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.service.langgraph_gateway.compatibility import CompatibilityResult
from zeroth.service.langgraph_gateway.context import ReservedContextCodec
from zeroth.service.langgraph_gateway.proxy import GatewayProxy
from zeroth.service.langgraph_gateway.transport import HTTPGatewayTransport

from .cases import ConformanceCase

EXPECTED_GOVERNANCE_ADDITIONS = (
    "response.header.x-correlation-id",
    "response.header.x-zeroth-governance-level",
    "forwarded.config.configurable._zeroth",
    "audit.langgraph.gateway",
)

_ADDITION_HEADERS = {"x-correlation-id", "x-zeroth-governance-level"}
_TRANSPORT_DERIVED_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


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
    retry_attempts: tuple[int, ...] = ()
    disconnect_outcome: str | None = None
    forwarded_context_present: bool = False
    audit_event_present: bool = False


@dataclass(frozen=True, slots=True)
class DifferentialReport:
    semantic_divergences: list[str]
    expected_governance_additions: list[str]
    first_divergence_path: str | None = None

    def write_human_report(self, path: Path) -> None:
        path.write_text(
            "LangGraph gateway differential report\n"
            f"semantic divergences: {self.semantic_divergences or ['none']}\n"
            f"first divergence: {self.first_divergence_path or 'none'}\n"
            f"expected governance additions: {self.expected_governance_additions or ['none']}\n",
            encoding="utf-8",
        )


@dataclass(frozen=True, slots=True)
class GeneratedValueMap:
    direct: tuple[tuple[str, str], ...] = ()
    proxied: tuple[tuple[str, str], ...] = ()
    direct_sse_ids: tuple[str | None, ...] = ()
    proxied_sse_ids: tuple[str | None, ...] = ()
    direct_sse_values: tuple[tuple[tuple[str, str], ...], ...] = ()
    proxied_sse_values: tuple[tuple[tuple[str, str], ...], ...] = ()

    @classmethod
    def from_pairs(cls, pairs: Sequence[tuple[object, object]]) -> GeneratedValueMap:
        direct: list[tuple[str, str]] = []
        proxied: list[tuple[str, str]] = []
        unique_pairs = sorted(
            dict.fromkeys((str(left), str(right)) for left, right in pairs),
            key=lambda pair: max(len(pair[0]), len(pair[1])),
            reverse=True,
        )
        for index, (direct_value, proxied_value) in enumerate(unique_pairs):
            token = f"<generated:{index}>"
            direct.append((direct_value, token))
            proxied.append((proxied_value, token))
        return cls(tuple(direct), tuple(proxied))


# noqa comment: this function's complexity predates ZER-24. The import
# sweep changed only which module the names come from, not the body.
def _normalize_generated(value: Any, replacements: tuple[tuple[str, str], ...]) -> Any:  # noqa: C901
    if isinstance(value, Mapping):
        return {key: _normalize_generated(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_generated(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_generated(item, replacements) for item in value)
    if isinstance(value, bytes):
        normalized = value
        for original, token in replacements:
            normalized = normalized.replace(original.encode(), token.encode())
        return normalized
    if isinstance(value, str):
        normalized = value
        for original, token in replacements:
            normalized = normalized.replace(original, token)
        return normalized
    if isinstance(value, int):
        for original, token in replacements:
            if str(value) == original:
                return token
    return value


def _without_reserved_context(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_reserved_context(item) for key, item in value.items() if key != "_zeroth"
        }
    if isinstance(value, list):
        return [_without_reserved_context(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_reserved_context(item) for item in value)
    return value


def _semantic_projection(
    capture: CapturedExchange,
    *,
    proxied: bool,
    replacements: tuple[tuple[str, str], ...],
    sse_ids: tuple[str | None, ...],
    sse_values: tuple[tuple[tuple[str, str], ...], ...],
) -> dict[str, Any]:
    def normalize(value: Any) -> Any:
        if proxied:
            value = _without_reserved_context(value)
        return _normalize_generated(value, replacements)

    headers = tuple(
        (name.lower(), normalize(value))
        for name, value in capture.headers
        if name.lower() not in _TRANSPORT_DERIVED_HEADERS
        and not (proxied and name.lower() in _ADDITION_HEADERS)
    )
    raw_chunks: Any = capture.raw_chunks
    if sse_ids:
        raw_chunks = tuple(
            _normalize_generated(
                _normalize_sse_frame_id(frame, frame_id, index),
                frame_replacements,
            )
            for index, (frame, frame_id, frame_replacements) in enumerate(
                zip(capture.raw_chunks, sse_ids, sse_values, strict=True)
            )
        )
    if len(capture.raw_chunks) == 1 and capture.final_json is not None:
        raw_json = _without_reserved_context(capture.final_json) if proxied else capture.final_json
        raw_chunks = (
            json.dumps(
                raw_json,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
    return {
        "status_code": capture.status_code,
        "headers": headers,
        "raw_chunks": normalize(raw_chunks),
        "final_json": normalize(capture.final_json),
        "state": normalize(capture.state),
        "interrupts": normalize(capture.interrupts),
        "resume_values": normalize(capture.resume_values),
        "tool_sequence": normalize(capture.tool_sequence),
        "errors": normalize(capture.errors),
        "cancellation_outcome": capture.cancellation_outcome,
        "disconnect_outcome": capture.disconnect_outcome,
        "retry_attempts": capture.retry_attempts,
        "terminal_state": capture.terminal_state,
    }


def _normalize_sse_frame_id(frame: bytes, frame_id: str | None, index: int) -> bytes:
    if frame_id is None:
        return frame
    expected = f"id: {frame_id}".encode()
    normalized: list[bytes] = []
    for line in frame.splitlines(keepends=True):
        ending = line[len(line.rstrip(b"\r\n")) :]
        content = line[: len(line) - len(ending)] if ending else line
        if content == expected:
            content = f"id: <generated-sse-id:{index}>".encode()
        normalized.append(content + ending)
    return b"".join(normalized)


def _json_pointer(path: tuple[str, ...]) -> str:
    pointer = "/" + "/".join(
        segment.replace("~", "~0").replace("/", "~1") for segment in path
    )
    return f"#{quote(pointer, safe='/~')}"


def _first_divergence_path(
    direct: Any,
    proxied: Any,
    path: tuple[str, ...] = (),
) -> str | None:
    if isinstance(direct, Mapping) and isinstance(proxied, Mapping):
        keys = sorted(set(direct) | set(proxied), key=lambda key: str(key))
        for key in keys:
            child_path = (*path, str(key))
            if key not in direct or key not in proxied:
                return _json_pointer(child_path)
            divergence = _first_divergence_path(direct[key], proxied[key], child_path)
            if divergence is not None:
                return divergence
        return None
    if type(direct) is type(proxied) and isinstance(direct, (list, tuple)):
        for index, (direct_item, proxied_item) in enumerate(zip(direct, proxied, strict=False)):
            divergence = _first_divergence_path(
                direct_item,
                proxied_item,
                (*path, str(index)),
            )
            if divergence is not None:
                return divergence
        if len(direct) != len(proxied):
            return _json_pointer((*path, str(min(len(direct), len(proxied)))))
        return None
    return None if direct == proxied else _json_pointer(path)


def compare_exchanges(
    direct: CapturedExchange,
    proxied: CapturedExchange,
    *,
    generated_values: GeneratedValueMap | None = None,
) -> DifferentialReport:
    generated_values = generated_values or GeneratedValueMap()
    direct_projection = _semantic_projection(
        direct,
        proxied=False,
        replacements=generated_values.direct,
        sse_ids=generated_values.direct_sse_ids,
        sse_values=generated_values.direct_sse_values,
    )
    proxied_projection = _semantic_projection(
        proxied,
        proxied=True,
        replacements=generated_values.proxied,
        sse_ids=generated_values.proxied_sse_ids,
        sse_values=generated_values.proxied_sse_values,
    )
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
    return DifferentialReport(
        divergences,
        additions,
        _first_divergence_path(direct_projection, proxied_projection),
    )


class _AllowPolicy:
    def evaluate_run_admission(self, request: object) -> RunAdmissionResult:
        return RunAdmissionResult(allowed=True, policy_version="sha256:conformance")


class _AllowBudget:
    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        return BudgetCheckResult(allowed=True, spend_usd=0, cap_usd=100)


class _RecordingSink:
    async def emit(self, event: object) -> None:
        return None


class _ObservedEvidenceProvider:
    async def evidence_for_governance_run(
        self, governance_run_id: str
    ) -> RunCapabilityEvidence:
        return RunCapabilityEvidence(
            correlation_id="conformance-correlation",
            run_id=governance_run_id,
            governance_level=GovernanceLevel.OBSERVED,
            observed_at=datetime.now(tz=UTC),
            graph_version="graph:conformance",
            signature_valid=True,
        )


def _append_evidence(path: str | None, row: dict[str, Any]) -> None:
    if path is None:
        return
    with Path(path).open("a", encoding="utf-8") as evidence:
        evidence.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


class _FileRecordingSink:
    def __init__(self, path: str) -> None:
        self._path = path

    async def emit(self, event: Any) -> None:
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else {}
        _append_evidence(self._path, {"kind": "audit", "event": payload})


class _CapturingTransport(HTTPGatewayTransport):
    def __init__(self, *args: Any, evidence_path: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._evidence_path = evidence_path

    async def forward(self, request: Request, *, tenant_id: str | None = None):
        raw = await request.body()
        present = False
        try:
            payload = json.loads(raw) if raw else None
            target = (
                payload.get("params")
                if isinstance(payload, dict) and "params" in payload
                else payload
            )
            present = bool(
                isinstance(target, dict)
                and isinstance(target.get("config"), dict)
                and isinstance(target["config"].get("configurable"), dict)
                and target["config"]["configurable"].get("_zeroth")
            )
        except (TypeError, ValueError):
            pass
        _append_evidence(
            self._evidence_path,
            {"kind": "forwarded", "path": request.url.path, "zeroth_present": present},
        )
        return await super().forward(request, tenant_id=tenant_id)


def _principal(_: Request) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject="conformance-user",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.OPERATOR],
        tenant_id="conformance-tenant",
    )


def create_gateway_app(
    upstream_url: str,
    *,
    event_sink: Any | None = None,
    evidence_path: str | None = None,
) -> tuple[Starlette, HTTPGatewayTransport]:
    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url=upstream_url,
        upstream_audience="agent-server:conformance",
        deployment_ref="conformance-deployment",
    )
    transport = _CapturingTransport(
        settings,
        EnvSecretProvider(),
        evidence_path=evidence_path,
    )
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
        capability_reporter=CapabilityReporter(
            governance_evidence_provider=_ObservedEvidenceProvider(),
            expected_graph_version="graph:conformance",
        ),
        event_sink=event_sink
        or (_FileRecordingSink(evidence_path) if evidence_path else _RecordingSink()),
        principal_resolver=_principal,
        correlation_factory=lambda: "conformance-correlation",
    )

    async def route(request: Request) -> Response:
        if request.url.path not in {"/ok", "/info", "/openapi.json"}:
            supplied_key = request.headers.get("x-api-key")
            supplied_bearer = request.headers.get("authorization")
            if supplied_key != "gateway-key" and supplied_bearer != "Bearer gateway-key":
                return JSONResponse(
                    {
                        "code": "zeroth.authentication_required",
                        "correlation_id": "conformance-auth",
                        "retryable": False,
                        "reason": "authentication required",
                    },
                    status_code=401,
                )
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


def run_gateway(upstream_url: str, port: int, evidence_path: str | None = None) -> None:
    app, _ = create_gateway_app(upstream_url, evidence_path=evidence_path)
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
        self.proxy_upstream_url = ""
        self._stack = ExitStack()
        self._processes: list[subprocess.Popen[bytes]] = []
        self.evidence_path = ""

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

        proxy_upstream_port = _free_port()
        proxy_upstream_dir = self._stack.enter_context(
            tempfile.TemporaryDirectory(prefix="zeroth-proxy-agent-")
        )
        proxy_upstream_script = (
            "from langgraph_api.cli import run_server; "
            f"run_server(host='127.0.0.1', port={proxy_upstream_port}, reload=False, "
            "graphs={'conformance':'tests.langgraph_gateway.conformance.graph:graph'}, "
            "disable_persistence=True, open_browser=False, server_level='ERROR')"
        )
        proxy_upstream = subprocess.Popen(
            [sys.executable, "-c", proxy_upstream_script],
            cwd=proxy_upstream_dir,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._processes.append(proxy_upstream)
        self.proxy_upstream_url = f"http://127.0.0.1:{proxy_upstream_port}"
        _wait_ready(self.proxy_upstream_url, proxy_upstream)

        gateway_port = _free_port()
        gateway_dir = self._stack.enter_context(
            tempfile.TemporaryDirectory(prefix="zeroth-conformance-gateway-")
        )
        self.evidence_path = str(Path(gateway_dir) / "evidence.jsonl")
        gateway_script = (
            "from tests.langgraph_gateway.conformance.harness import run_gateway; "
            f"run_gateway({self.proxy_upstream_url!r}, {gateway_port}, {self.evidence_path!r})"
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

    def evidence(self, *, since: int = 0) -> tuple[dict[str, Any], ...]:
        path = Path(self.evidence_path)
        if not path.exists():
            return ()
        rows = tuple(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        )
        return rows[since:]

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


@dataclass(frozen=True, slots=True)
class PairedCaseResult:
    direct: CapturedExchange
    proxied: CapturedExchange
    generated_values: GeneratedValueMap


def _render_case_value(value: Any, context: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(context)
    if isinstance(value, dict):
        return {key: _render_case_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_case_value(item, context) for item in value]
    return value


def _prepare_case_context(
    client: httpx.Client,
    base_url: str,
    case: ConformanceCase,
    headers: Mapping[str, str],
) -> tuple[dict[str, str], bool]:
    if case.group == "system":
        return {}, False
    assistant = client.post(
        f"{base_url}/assistants",
        json={
            "graph_id": "conformance",
            "name": f"pair-{case.name}",
            "metadata": {"pair": case.name},
        },
        headers=headers,
    )
    assistant.raise_for_status()
    thread = client.post(
        f"{base_url}/threads",
        json={"metadata": {"pair": case.name}},
        headers=headers,
    )
    thread.raise_for_status()
    context = {
        "assistant_id": assistant.json()["assistant_id"],
        "thread_id": thread.json()["thread_id"],
    }
    initialized = case.group in {
        "threads",
        "state",
        "history",
        "run-get",
        "run-join",
        "run-join-stream",
        "run-cancel",
        "event-stream",
    }
    if initialized:
        mode = "cancel" if case.group == "run-cancel" else "echo"
        run = client.post(
            f"{base_url}/threads/{context['thread_id']}/runs",
            json={
                "assistant_id": context["assistant_id"],
                "input": {"mode": mode, "text": "paired-setup"},
                "stream_mode": ["custom", "values"],
            },
            headers=headers,
        )
        run.raise_for_status()
        context["run_id"] = run.json()["run_id"]
        if mode != "cancel":
            joined = client.get(
                f"{base_url}/threads/{context['thread_id']}/runs/{context['run_id']}/join",
                headers=headers,
            )
            joined.raise_for_status()
            state = client.get(f"{base_url}/threads/{context['thread_id']}/state", headers=headers)
            state.raise_for_status()
            context["checkpoint_id"] = state.json()["checkpoint"]["checkpoint_id"]
    if case.name in {"native-resume", "protocol-input-respond"}:
        paused = client.post(
            f"{base_url}/threads/{context['thread_id']}/runs/wait",
            json={
                "assistant_id": context["assistant_id"],
                "input": {"mode": "interrupt", "text": "approve"},
            },
            headers=headers,
        )
        paused.raise_for_status()
        initialized = True
    return context, initialized


def _capture_case_request(
    client: httpx.Client,
    base_url: str,
    case: ConformanceCase,
    context: Mapping[str, str],
    headers: Mapping[str, str],
    *,
    initialized: bool,
    evidence: tuple[dict[str, Any], ...] = (),
) -> CapturedExchange:
    payload = _render_case_value(case.request(context), context)
    disconnect_outcome: str | None = None
    if case.expected_content_type == "text/event-stream" and case.group in {
        "threads",
        "event-stream",
    }:
        captured_response, raw_chunks, disconnect_outcome = _capture_subscription_frames(
            client,
            base_url,
            case,
            context,
            headers,
            payload,
        )
    else:
        with client.stream(
            case.method,
            f"{base_url}{case.path(context)}",
            json=payload,
            headers=headers,
            timeout=15,
        ) as response:
            if case.expected_content_type == "text/event-stream":
                raw_chunks = tuple(_exact_sse_frames(response.iter_raw()))
            else:
                raw_chunks = (response.read(),)
            captured_response = httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(raw_chunks),
                request=response.request,
            )
    response_body = b"".join(raw_chunks)

    final_json: Any = None
    if response_body and captured_response.headers.get("content-type", "").startswith(
        "application/json"
    ):
        final_json = captured_response.json()
    state: Any = None
    if initialized and "thread_id" in context and case.group != "run-cancel":
        state_response = client.get(
            f"{base_url}/threads/{context['thread_id']}/state", headers=headers
        )
        if state_response.status_code == 200:
            history_response = client.post(
                f"{base_url}/threads/{context['thread_id']}/history",
                json={"limit": 10},
                headers=headers,
            )
            state = {
                "state": state_response.json(),
                "history": history_response.json() if history_response.status_code == 200 else None,
            }
    terminal_state: Any = "success" if captured_response.is_success else "error"
    cancellation_outcome: Any = None
    if case.group == "run-cancel" and "run_id" in context:
        run_response = client.get(
            f"{base_url}/threads/{context['thread_id']}/runs/{context['run_id']}",
            headers=headers,
        )
        if run_response.status_code == 200:
            terminal_state = run_response.json().get("status")
            cancellation_outcome = terminal_state
        join_response = client.get(
            f"{base_url}/threads/{context['thread_id']}/runs/{context['run_id']}/join",
            headers=headers,
        )
        state_response = client.get(
            f"{base_url}/threads/{context['thread_id']}/state", headers=headers
        )
        state = {
            "join_status": join_response.status_code,
            "join": join_response.json() if join_response.content else None,
            "state_status": state_response.status_code,
            "state": state_response.json() if state_response.content else None,
        }
    interrupts = ()
    resume_values = ()
    tool_sequence = ()
    retry_attempts = ()
    if isinstance(final_json, dict):
        interrupts = tuple(final_json.get("__interrupt__", ()))
        result = final_json.get("result")
        if isinstance(result, str) and result.startswith("resumed:"):
            resume_values = (result.removeprefix("resumed:"),)
        tool_sequence = tuple(final_json.get("tool_sequence", ()))
        retry_attempts = tuple(final_json.get("retry_attempts", ()))
    errors = (
        ()
        if captured_response.is_success
        else (final_json or response_body.decode(errors="replace"),)
    )
    return CapturedExchange(
        status_code=captured_response.status_code,
        headers=tuple(captured_response.headers.multi_items()),
        raw_chunks=raw_chunks,
        final_json=final_json,
        state=state,
        interrupts=interrupts,
        resume_values=resume_values,
        tool_sequence=tool_sequence,
        errors=errors,
        cancellation_outcome=cancellation_outcome,
        terminal_state=terminal_state,
        retry_attempts=retry_attempts,
        disconnect_outcome=disconnect_outcome,
        forwarded_context_present=any(
            row.get("kind") == "forwarded" and row.get("zeroth_present") for row in evidence
        ),
        audit_event_present=any(row.get("kind") == "audit" for row in evidence),
    )


def _exact_sse_frames(chunks: Iterator[bytes]) -> Iterator[bytes]:
    pending = b""
    for chunk in chunks:
        pending += chunk
        while True:
            endings = [
                (index, delimiter)
                for delimiter in (b"\r\n\r\n", b"\n\n")
                if (index := pending.find(delimiter)) >= 0
            ]
            if not endings:
                break
            index, delimiter = min(endings, key=lambda item: item[0])
            frame_end = index + len(delimiter)
            yield pending[:frame_end]
            pending = pending[frame_end:]


def _capture_subscription_frames(
    trigger_client: httpx.Client,
    base_url: str,
    case: ConformanceCase,
    context: Mapping[str, str],
    headers: Mapping[str, str],
    payload: Any,
) -> tuple[httpx.Response, tuple[bytes, ...], str]:
    frame_limit = 6 if case.group == "threads" else 4
    ready = threading.Event()
    finished = threading.Event()
    result: dict[str, Any] = {}

    def subscribe() -> None:
        try:
            with httpx.Client(timeout=httpx.Timeout(15, read=15)) as subscriber:
                with subscriber.stream(
                    case.method,
                    f"{base_url}{case.path(context)}",
                    json=payload,
                    headers=headers,
                ) as response:
                    result["response"] = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        request=response.request,
                    )
                    frames = _exact_sse_frames(response.iter_raw())
                    if case.group == "event-stream":
                        next(frames)
                    ready.set()
                    result["frames"] = tuple(islice(frames, frame_limit))
        except BaseException as exc:
            result["error"] = exc
            ready.set()
        finally:
            finished.set()

    subscriber_thread = threading.Thread(target=subscribe, daemon=True)
    subscriber_thread.start()
    if not ready.wait(timeout=10):
        raise TimeoutError(f"subscription did not become ready: {case.name}")
    if "error" in result:
        raise result["error"]

    triggered = trigger_client.post(
        f"{base_url}/threads/{context['thread_id']}/runs/wait",
        json={
            "assistant_id": context["assistant_id"],
            "input": {"mode": "echo", "text": "subscription-trigger"},
            "stream_mode": ["custom", "values"],
        },
        headers=headers,
        timeout=15,
    )
    triggered.raise_for_status()
    if not finished.wait(timeout=15):
        raise TimeoutError(f"subscription did not yield {frame_limit} frames: {case.name}")
    if "error" in result:
        raise result["error"]
    frames = result.get("frames", ())
    if len(frames) != frame_limit:
        raise AssertionError(
            f"subscription yielded {len(frames)} of {frame_limit} frames: {case.name}"
        )
    return result["response"], frames, "client_stream_closed"


def _matching_generated_pairs(
    direct: Any,
    proxied: Any,
    *,
    allowed_keys: frozenset[str],
) -> list[tuple[object, object]]:
    pairs: list[tuple[object, object]] = []
    if isinstance(direct, Mapping) and isinstance(proxied, Mapping):
        for key in direct.keys() & proxied.keys():
            if (
                key in allowed_keys
                and isinstance(direct[key], (int, str))
                and isinstance(proxied[key], (int, str))
            ):
                pairs.append((direct[key], proxied[key]))
            else:
                pairs.extend(
                    _matching_generated_pairs(direct[key], proxied[key], allowed_keys=allowed_keys)
                )
    elif isinstance(direct, list) and isinstance(proxied, list):
        for direct_item, proxied_item in zip(direct, proxied, strict=False):
            pairs.extend(
                _matching_generated_pairs(direct_item, proxied_item, allowed_keys=allowed_keys)
            )
    return pairs


def _sse_payloads(chunks: tuple[bytes, ...]) -> list[Any]:
    payloads: list[Any] = []
    for frame in chunks:
        for line in frame.splitlines():
            if not line.startswith(b"data: "):
                continue
            try:
                payloads.append(json.loads(line.removeprefix(b"data: ")))
            except (TypeError, ValueError):
                continue
    return payloads


def _sse_payload(frame: bytes) -> Any:
    payloads = _sse_payloads((frame,))
    return payloads[0] if payloads else None


def _sse_frame_ids(chunks: tuple[bytes, ...]) -> list[str | None]:
    return [
        next(
            (
                line.removeprefix(b"id: ").decode()
                for line in frame.splitlines()
                if line.startswith(b"id: ")
            ),
            None,
        )
        for frame in chunks
    ]


def execute_paired_case(servers: ConformanceServers, case: ConformanceCase) -> PairedCaseResult:
    direct_headers: dict[str, str] = {}
    gateway_headers = {"x-api-key": "gateway-key"}
    with httpx.Client(timeout=15) as client:
        direct_context, direct_initialized = _prepare_case_context(
            client, servers.direct_url, case, direct_headers
        )
        proxy_context, proxy_initialized = _prepare_case_context(
            client, servers.gateway_url, case, gateway_headers
        )
        direct = _capture_case_request(
            client,
            servers.direct_url,
            case,
            direct_context,
            direct_headers,
            initialized=direct_initialized,
        )
        evidence_start = len(servers.evidence())
        active_gateway_headers = {} if case.group == "auth" else gateway_headers
        proxied = _capture_case_request(
            client,
            servers.gateway_url,
            case,
            proxy_context,
            active_gateway_headers,
            initialized=proxy_initialized,
            evidence=(),
        )
        evidence = servers.evidence(since=evidence_start)
        deadline = time.monotonic() + 1
        while (
            not any(row.get("kind") == "audit" for row in evidence) and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            evidence = servers.evidence(since=evidence_start)
        proxied = CapturedExchange(
            **{
                **asdict(proxied),
                "forwarded_context_present": any(
                    row.get("kind") == "forwarded" and row.get("zeroth_present") for row in evidence
                ),
                "audit_event_present": any(row.get("kind") == "audit" for row in evidence),
            }
        )

    pairs: list[tuple[object, object]] = list(
        zip(direct_context.values(), proxy_context.values(), strict=False)
    )
    direct_headers_map = dict(direct.headers)
    proxy_headers_map = dict(proxied.headers)
    for name in ("date", "location", "content-location"):
        if name in direct_headers_map and name in proxy_headers_map:
            pairs.append((direct_headers_map[name], proxy_headers_map[name]))
    allowed_keys = {
        "__request_start_time_ms__",
        "created_at",
        "event_id",
        "langgraph_request_id",
        "updated_at",
    }
    if case.name == "system-openapi":
        allowed_keys.add("url")
    if case.name in {
        "assistant-create",
        "thread-create",
        "threaded-background",
        "protocol-run-start",
    }:
        allowed_keys.update({"assistant_id", "thread_id", "run_id"})
    if case.group == "interrupt-resume":
        allowed_keys.add("id")
    pairs.extend(
        _matching_generated_pairs(
            direct.final_json,
            proxied.final_json,
            allowed_keys=frozenset(allowed_keys),
        )
    )
    pairs.extend(
        _matching_generated_pairs(
            direct.state,
            proxied.state,
            allowed_keys=frozenset(
                {
                    "checkpoint_id",
                    "created_at",
                    "id",
                    "langgraph_api_url",
                    "langgraph_request_id",
                    "parent_checkpoint_id",
                    "run_id",
                    "updated_at",
                }
            ),
        )
    )
    generated_values = GeneratedValueMap.from_pairs(pairs)
    sse_value_maps = [
        GeneratedValueMap.from_pairs(
            _matching_generated_pairs(
                _sse_payload(direct_frame),
                _sse_payload(proxy_frame),
                allowed_keys=frozenset(
                    {"created_at", "event_id", "run_id", "timestamp", "updated_at"}
                ),
            )
        )
        for direct_frame, proxy_frame in zip(direct.raw_chunks, proxied.raw_chunks, strict=True)
    ]
    generated_values = GeneratedValueMap(
        direct=generated_values.direct,
        proxied=generated_values.proxied,
        direct_sse_ids=tuple(_sse_frame_ids(direct.raw_chunks)),
        proxied_sse_ids=tuple(_sse_frame_ids(proxied.raw_chunks)),
        direct_sse_values=tuple(mapping.direct for mapping in sse_value_maps),
        proxied_sse_values=tuple(mapping.proxied for mapping in sse_value_maps),
    )
    return PairedCaseResult(direct, proxied, generated_values)


def capture_response(
    response: httpx.Response,
    *,
    state: Any = None,
    raw_chunks: tuple[bytes, ...] | None = None,
    forwarded_context_present: bool = False,
    audit_event_present: bool = False,
) -> CapturedExchange:
    content_type = response.headers.get("content-type", "")
    final_json = response.json() if content_type.startswith("application/json") else None
    retry_attempts = (
        tuple(final_json.get("retry_attempts", ())) if isinstance(final_json, dict) else ()
    )
    return CapturedExchange(
        status_code=response.status_code,
        headers=tuple(response.headers.multi_items()),
        raw_chunks=raw_chunks if raw_chunks is not None else (response.content,),
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
        retry_attempts=retry_attempts,
        forwarded_context_present=forwarded_context_present,
        audit_event_present=audit_event_present,
    )
