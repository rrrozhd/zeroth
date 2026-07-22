from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


Context = Mapping[str, str]
PathBuilder = Callable[[Context], str]
RequestBuilder = Callable[[Context], dict[str, Any] | None]
Normalizer = Callable[[Any], Any]
Cleanup = Callable[[Any, Context], None]


def _identity(value: Any) -> Any:
    return value


def _stable_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_json(item)
            for key, item in sorted(value.items())
            if key not in {"created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_stable_json(item) for item in value]
    return value


def _noop_cleanup(_: Any, __: Context) -> None:
    return None


@dataclass(frozen=True, slots=True)
class ConformanceCase:
    name: str
    group: str
    method: str
    path: PathBuilder
    request: RequestBuilder
    expected_status: tuple[int, ...]
    expected_content_type: str
    normalizer: Normalizer
    governance: str
    cleanup: Cleanup


REQUIRED_OPERATION_GROUPS = {
    "assistants",
    "threads",
    "state",
    "history",
    "runs-background",
    "runs-wait",
    "runs-stream",
    "stateless-wait",
    "stateless-stream",
    "run-get",
    "run-join",
    "run-join-stream",
    "run-cancel",
    "protocol-v2",
    "event-stream",
    "interrupt-resume",
    "auth",
    "validation",
    "unsupported",
}


def _case(
    name: str,
    group: str,
    method: str,
    path: str,
    request: dict[str, Any] | None,
    *,
    status: tuple[int, ...] = (200,),
    content_type: str = "application/json",
    governance: str = "transparent",
) -> ConformanceCase:
    return ConformanceCase(
        name=name,
        group=group,
        method=method,
        path=lambda context, template=path: template.format_map(context),
        request=lambda _context, payload=request: payload,
        expected_status=status,
        expected_content_type=content_type,
        normalizer=_stable_json if content_type == "application/json" else _identity,
        governance=governance,
        cleanup=_noop_cleanup,
    )


_RUN = {"assistant_id": "{assistant_id}", "input": {"mode": "echo", "text": "hello"}}
_STREAM_RUN = {**_RUN, "stream_mode": ["custom", "values"]}


CASES = (
    _case(
        "assistant-create",
        "assistants",
        "POST",
        "/assistants",
        {"graph_id": "conformance", "name": "fixture"},
    ),
    _case("assistant-read", "assistants", "GET", "/assistants/{assistant_id}", None),
    _case("assistant-graph", "assistants", "GET", "/assistants/{assistant_id}/graph", None),
    _case(
        "assistant-search",
        "assistants",
        "POST",
        "/assistants/search",
        {"graph_id": "conformance", "limit": 10},
    ),
    _case("thread-create", "threads", "POST", "/threads", {"metadata": {"fixture": True}}),
    _case("thread-read", "threads", "GET", "/threads/{thread_id}", None),
    _case(
        "thread-search",
        "threads",
        "POST",
        "/threads/search",
        {"metadata": {"fixture": True}, "limit": 10},
    ),
    _case(
        "thread-stream",
        "threads",
        "GET",
        "/threads/{thread_id}/stream",
        None,
        content_type="text/event-stream",
    ),
    _case("state-read", "state", "GET", "/threads/{thread_id}/state", None),
    _case(
        "state-update",
        "state",
        "POST",
        "/threads/{thread_id}/state",
        {"values": {"text": "updated"}},
    ),
    _case(
        "state-checkpoint",
        "state",
        "POST",
        "/threads/{thread_id}/state/checkpoint",
        {"checkpoint": {"checkpoint_id": "{checkpoint_id}"}},
    ),
    _case("history", "history", "POST", "/threads/{thread_id}/history", {"limit": 10}),
    _case(
        "threaded-background",
        "runs-background",
        "POST",
        "/threads/{thread_id}/runs",
        _RUN,
        governance="governed",
    ),
    _case(
        "threaded-wait",
        "runs-wait",
        "POST",
        "/threads/{thread_id}/runs/wait",
        _RUN,
        governance="governed",
    ),
    _case(
        "threaded-stream",
        "runs-stream",
        "POST",
        "/threads/{thread_id}/runs/stream",
        _STREAM_RUN,
        content_type="text/event-stream",
        governance="governed",
    ),
    _case("stateless-wait", "stateless-wait", "POST", "/runs/wait", _RUN, governance="governed"),
    _case(
        "stateless-stream",
        "stateless-stream",
        "POST",
        "/runs/stream",
        _STREAM_RUN,
        content_type="text/event-stream",
        governance="governed",
    ),
    _case("run-get", "run-get", "GET", "/threads/{thread_id}/runs/{run_id}", None),
    _case("run-join", "run-join", "GET", "/threads/{thread_id}/runs/{run_id}/join", None),
    _case(
        "run-join-stream",
        "run-join-stream",
        "GET",
        "/threads/{thread_id}/runs/{run_id}/stream",
        None,
        content_type="text/event-stream",
    ),
    _case(
        "run-cancel",
        "run-cancel",
        "POST",
        "/threads/{thread_id}/runs/{run_id}/cancel",
        {"wait": True},
        status=(202,),
        content_type="none",
    ),
    _case(
        "protocol-run-start",
        "protocol-v2",
        "POST",
        "/threads/{thread_id}/commands",
        {"id": 1, "method": "run.start", "params": _RUN},
        governance="governed",
    ),
    _case(
        "protocol-input-respond",
        "protocol-v2",
        "POST",
        "/threads/{thread_id}/commands",
        {"id": 2, "method": "input.respond", "params": {"command": {"resume": "approved"}}},
        status=(200, 400, 404, 409),
        governance="governed",
    ),
    _case(
        "protocol-event-stream",
        "event-stream",
        "POST",
        "/threads/{thread_id}/stream/events",
        {"channels": ["values", "custom"]},
        content_type="text/event-stream",
    ),
    _case(
        "native-interrupt",
        "interrupt-resume",
        "POST",
        "/threads/{thread_id}/runs/wait",
        {"assistant_id": "{assistant_id}", "input": {"mode": "interrupt", "text": "approve"}},
        governance="governed",
    ),
    _case(
        "native-resume",
        "interrupt-resume",
        "POST",
        "/threads/{thread_id}/runs/wait",
        {"assistant_id": "{assistant_id}", "command": {"resume": "approved"}},
        governance="governed",
    ),
    _case("auth-error", "auth", "GET", "/assistants/{assistant_id}", None, status=(200, 401, 403)),
    _case(
        "validation-error",
        "validation",
        "POST",
        "/threads/{thread_id}/runs/wait",
        {"assistant_id": 7},
        status=(400, 422),
    ),
    _case(
        "unsupported-crons",
        "unsupported",
        "POST",
        "/runs/crons",
        {
            "assistant_id": "{assistant_id}",
            "schedule": "0 0 1 1 *",
            "input": {"mode": "echo", "text": "cron"},
            "enabled": False,
        },
        status=(200,),
        governance="unsupported",
    ),
    _case(
        "unsupported-a2a",
        "unsupported",
        "POST",
        "/a2a",
        {},
        status=(404, 405, 422),
        governance="unsupported",
    ),
    _case(
        "unsupported-mcp",
        "unsupported",
        "POST",
        "/mcp/",
        {},
        status=(307,),
        content_type="none",
        governance="unsupported",
    ),
    _case(
        "unsupported-store",
        "unsupported",
        "GET",
        "/store/items?namespace=fixture&key=missing",
        None,
        status=(200,),
        governance="unsupported",
    ),
    _case(
        "unsupported-custom",
        "unsupported",
        "GET",
        "/custom/fixture",
        None,
        status=(404,),
        governance="unsupported",
    ),
)
