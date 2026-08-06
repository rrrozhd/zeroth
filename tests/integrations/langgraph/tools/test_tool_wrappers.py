"""Proof that ``govern_tools`` governs every surface and mutates none of them.

**The compatibility matrix is the spine of this file.** Eight cells --
``BaseTool`` x plain callable, sync x async, ``args_schema`` present x absent --
each get one ``test_cell_*`` named after the cell it covers, so the documented
matrix maps onto tests mechanically rather than by reading prose.

**Non-mutation is asserted by behaviour, never by ``is``.** A wrapper that
reassigned ``tool.func`` to a *governed* function would leave every identity
assertion intact, still execute, and still return the right answer on an allow.
The assertion that discriminates is that a second name bound to the original --
the tool object, its ``.func``, its ``.coroutine`` -- still runs **and the
decision client is never consulted**. A client call count of zero is what proves
the second reference reaches the original body and not a governed one.

**Every enforcement assertion counts invocations.** "Denied" is not "raised": it
is "raised *and* the body ran zero times". A wrapper that invoked first and
raised afterwards satisfies every ``pytest.raises`` in a suite that does not
count.

``langchain-core`` is a core dependency of this package and is already imported
eagerly by ``_wrapper.py`` / ``_handler.py``, so real ``BaseTool`` objects are
used here. Nothing imports ``langchain.agents`` (the new optional dependency,
which is the middleware suite's), nothing needs ``langgraph`` -- the pause seam is
injected -- and so nothing here carries the ``langgraph_conformance`` marker:
``addopts`` deselects it and a marked test would never run.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import subprocess
import sys
import tempfile
from typing import Annotated, Any

import pydantic
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import (
    BaseTool,
    InjectedToolCallId,
    StructuredTool,
    ToolException,
    tool,
)
from pydantic import BaseModel, ConfigDict

from tests.integrations.langgraph.tools._hostile import (
    CONTENT_SENTINEL,
    HostileDict,
    HostileList,
    HostileStr,
)
from zeroth.governance.audit import NodeAuditRecord
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.langgraph._approval_lifecycle import (
    ApprovalDecision,
    ApprovalResolution,
    ApprovalState,
    SQLiteApprovalRepository,
)
from zeroth.integrations.langgraph._tool_decisions import UnknownSideEffectPolicy
from zeroth.integrations.langgraph._tool_errors import (
    GovernanceContextError,
    PolicyViolation,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)
from zeroth.integrations.langgraph._tool_wrappers import GovernedTool, govern_tools

THREADED = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
    correlation_id="corr-1",
)
"""A run that can be paused: approval needs a thread to resume into."""

THREADLESS = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
DENY = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")
APPROVE = ToolDecision(
    kind=ToolDecisionKind.REQUIRE_APPROVAL,
    reason_code="policy_violation",
    approval_ref="approval-7",
)


@dataclasses.dataclass
class CountingClient:
    """A decision client that returns a fixed verdict and counts every consultation."""

    verdict: object = ALLOW
    calls: int = 0
    seen: list[ToolAction] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Record the consultation and return the configured verdict."""
        self.calls += 1
        self.seen.append(action)
        return self.verdict  # type: ignore[return-value]


@dataclasses.dataclass
class RecordingInterrupt:
    """A pause seam that records the payload and suspends by raising, as LangGraph does."""

    payloads: list[Any] = dataclasses.field(default_factory=list)

    def __call__(self, payload: Any) -> Any:
        """Record *payload* and suspend."""
        self.payloads.append(payload)
        raise Suspended


class Suspended(Exception):  # noqa: N818 - a pause, not a malfunction.
    """Stands in for LangGraph's ``GraphInterrupt``, which is what a real pause raises."""


@dataclasses.dataclass
class RecordingSubmitter:
    """An audit sink that keeps every record the enforcement core handed it."""

    records: list[NodeAuditRecord] = dataclasses.field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> None:
        """Keep *record*, as the delivery queue's non-blocking hand-off does."""
        self.records.append(record)


@dataclasses.dataclass
class ApprovalReplayClient:
    """Require the initial and replayed interrupt, then revalidate as allowed."""

    seen: list[ToolAction] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        del context
        self.seen.append(action)
        return ALLOW if len(self.seen) >= 3 else APPROVE


@dataclasses.dataclass
class DefaultDenyReplayClient:
    """Require approval once, then deny the exact edited call revalidation."""

    seen: list[ToolAction] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        del context
        self.seen.append(action)
        return APPROVE if len(self.seen) == 1 else DENY


@dataclasses.dataclass
class ApprovalReplayInterrupt:
    """Suspend once, then deliver the repository's current fenced resolution."""

    payloads: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    delivery: dict[str, Any] | None = None
    before_delivery: Any = None

    def __call__(self, payload: Any) -> Any:
        self.payloads.append(dict(payload))
        if self.delivery is None:
            raise Suspended
        if self.before_delivery is not None:
            self.before_delivery()
        return dict(self.delivery)


def read_only(_target: object) -> SideEffectClass:
    """Classify every tool as read-only, so the allow path needs no blanket opt-in."""
    return SideEffectClass.READ_ONLY


class Body:
    """A tool body that counts every execution and can be reached by a second name."""

    def __init__(self, result: str = "body-result") -> None:
        self.calls = 0
        self.result = result

    def run(self, **kwargs: Any) -> str:
        """Count this execution and return the configured result."""
        self.calls += 1
        return self.result


class Args(BaseModel):
    """The declared schema for the tools whose cell has ``args_schema`` present."""

    table: str
    row: int


# --------------------------------------------------------------------------- #
# Tool builders, one per compatibility-matrix surface.
# --------------------------------------------------------------------------- #


def sync_tool_with_schema(body: Body) -> StructuredTool:
    """Build a sync ``BaseTool`` that declares an ``args_schema``."""

    def delete_row(table: str, row: int) -> str:
        """Delete one row."""
        return body.run(table=table, row=row)

    return StructuredTool.from_function(
        func=delete_row, name="delete_row", description="Delete one row.", args_schema=Args
    )


def async_tool_with_schema(body: Body) -> StructuredTool:
    """Build an async ``BaseTool`` that declares an ``args_schema``."""

    async def adelete_row(table: str, row: int) -> str:
        """Delete one row, asynchronously."""
        return body.run(table=table, row=row)

    return StructuredTool.from_function(
        coroutine=adelete_row,
        name="adelete_row",
        description="Delete one row, asynchronously.",
        args_schema=Args,
    )


class SchemalessTool(BaseTool):
    """A hand-built ``BaseTool`` with no ``args_schema``: the framework's string-input shape."""

    name: str = "schemaless"
    description: str = "Takes one bare string."
    args_schema: Any = None
    body: Any = None

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Count this execution and echo the single positional input."""
        self.body.calls += 1
        return f"sync:{args[0]}"


class AsyncSchemalessTool(BaseTool):
    """A hand-built async ``BaseTool`` with no ``args_schema``."""

    name: str = "aschemaless"
    description: str = "Takes one bare string, asynchronously."
    args_schema: Any = None
    body: Any = None

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """Refuse the sync path, exactly as an async-only tool does."""
        raise NotImplementedError("async only")

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """Count this execution and echo the single positional input."""
        self.body.calls += 1
        return f"async:{args[0]}"


class DefaultingSchemalessTool(BaseTool):
    """A schema-less sync tool whose body supplies a dangerous default."""

    name: str = "defaulting"
    description: str = "Defaults to a dangerous path."
    args_schema: Any = None
    effects: Any = None

    def _run(self, path: str = "/danger") -> str:
        self.effects.append(path)
        return path


class AsyncDefaultingSchemalessTool(BaseTool):
    """Async twin of :class:`DefaultingSchemalessTool`."""

    name: str = "adefaulting"
    description: str = "Defaults to a dangerous path asynchronously."
    args_schema: Any = None
    effects: Any = None

    def _run(self, path: str = "/danger") -> str:
        raise NotImplementedError("async only")

    async def _arun(self, path: str = "/danger") -> str:
        self.effects.append(path)
        return path


def sync_callable_with_schema(body: Body) -> Any:
    """Build a plain sync callable that carries an ``args_schema`` attribute."""

    def delete_row(table: str, row: int) -> str:
        """Delete one row."""
        return body.run(table=table, row=row)

    delete_row.args_schema = Args  # type: ignore[attr-defined]
    return delete_row


def sync_callable_without_schema(body: Body) -> Any:
    """Build a plain sync callable that declares no schema at all."""

    def search(query: str) -> str:
        """Search for something."""
        return body.run(query=query)

    return search


def async_callable_with_schema(body: Body) -> Any:
    """Build a plain async callable that carries an ``args_schema`` attribute."""

    async def adelete_row(table: str, row: int) -> str:
        """Delete one row, asynchronously."""
        return body.run(table=table, row=row)

    adelete_row.args_schema = Args  # type: ignore[attr-defined]
    return adelete_row


def async_callable_without_schema(body: Body) -> Any:
    """Build a plain async callable that declares no schema at all."""

    async def asearch(query: str) -> str:
        """Search for something, asynchronously."""
        return body.run(query=query)

    return asearch


def wrap(target: Any, *, client: CountingClient, context: object = THREADED, **kwargs: Any) -> Any:
    """Govern one tool with a read-only classification, so an allow can be reached."""
    directory = tempfile.TemporaryDirectory()
    _LIFECYCLE_DIRS.append(directory)
    kwargs.setdefault(
        "approval_lifecycle", SQLiteApprovalRepository(f"{directory.name}/approvals.sqlite3")
    )
    return govern_tools([target], context=context, client=client, side_effect=read_only, **kwargs)[
        0
    ]


_LIFECYCLE_DIRS: list[tempfile.TemporaryDirectory[str]] = []


def prepare_base_tool_approval_replay(
    tmp_path: Any,
    original: BaseTool,
    call_input: Any,
    edited_arguments: dict[str, Any],
) -> tuple[
    GovernedTool,
    SQLiteApprovalRepository,
    ApprovalReplayClient,
    ApprovalReplayInterrupt,
]:
    """Pause a real BaseTool call and prepare its fenced edited replay."""
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    client = ApprovalReplayClient()
    interrupt = ApprovalReplayInterrupt()
    [governed] = govern_tools(
        [original],
        context=THREADED,
        client=client,
        side_effect=read_only,
        interrupt=interrupt,
        approval_lifecycle=repository,
    )
    with pytest.raises(Suspended):
        governed.invoke(call_input)
    repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE, edited_arguments)
    repository.decide(resolution)
    claimed = repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    interrupt.delivery = {
        **resolution.to_payload(),
        "claim_token": claimed.claim_token,
    }
    return governed, repository, client, interrupt


# --------------------------------------------------------------------------- #
# The compatibility matrix: BaseTool x callable, sync x async, schema x none.
# --------------------------------------------------------------------------- #


def test_cell_base_tool_sync_with_args_schema_preserves_interface_and_invokes_once() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert isinstance(governed, GovernedTool)
    assert governed.name == original.name
    assert governed.description == original.description
    assert governed.args_schema is original.args_schema
    assert governed.args == original.args
    assert governed.invoke({"table": "invoices", "row": 3}) == "body-result"
    assert body.calls == 1
    assert client.calls == 1


def test_cell_base_tool_sync_without_args_schema_preserves_interface_and_invokes_once() -> None:
    body = Body()
    original = SchemalessTool(body=body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == original.name
    assert governed.description == original.description
    assert governed.args_schema is None and original.args_schema is None
    # The delegate's own input schema is reported, not one inferred from the
    # wrapper's ``*args`` / ``**kwargs`` signature.
    assert governed.args == original.args
    assert governed.invoke("hello") == "sync:hello"
    assert body.calls == 1
    assert client.calls == 1
    # The bare positional input is decided under langchain-core's own name for it.
    assert dict(client.seen[0].arguments) == {"__arg1": "hello"}


def test_cell_base_tool_async_with_args_schema_preserves_interface_and_invokes_once() -> None:
    body = Body()
    original = async_tool_with_schema(body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == original.name
    assert governed.description == original.description
    assert governed.args_schema is original.args_schema
    assert asyncio.run(governed.ainvoke({"table": "invoices", "row": 3})) == "body-result"
    assert body.calls == 1
    assert client.calls == 1


def test_cell_base_tool_async_without_args_schema_preserves_interface_and_invokes_once() -> None:
    body = Body()
    original = AsyncSchemalessTool(body=body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == original.name
    assert governed.description == original.description
    assert governed.args_schema is None
    assert governed.args == original.args
    assert asyncio.run(governed.ainvoke("hello")) == "async:hello"
    assert body.calls == 1
    assert client.calls == 1


def test_cell_plain_callable_sync_with_args_schema_preserves_interface_and_invokes_once() -> None:
    body = Body()
    original = sync_callable_with_schema(body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == "delete_row"
    assert governed.description == original.__doc__
    assert governed.args_schema is not Args
    assert governed.args_schema.model_json_schema() == Args.model_json_schema()
    assert governed.args_schema.model_config == Args.model_config
    assert (
        governed.args_schema.model_validate({"table": "invoices", "row": 3}).model_dump()
        == Args(table="invoices", row=3).model_dump()
    )
    assert governed(table="invoices", row=3) == "body-result"
    assert body.calls == 1
    assert client.calls == 1


def test_cell_plain_callable_sync_without_args_schema_preserves_interface_and_invokes_once() -> (
    None
):
    body = Body()
    original = sync_callable_without_schema(body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == "search"
    assert governed.description == original.__doc__
    assert governed.args_schema is None
    # A bare callable's whole interface is the direct call, and a positional
    # argument is decided under its real parameter name, not ``__arg1``.
    assert governed("cats") == "body-result"
    assert dict(client.seen[0].arguments) == {"query": "cats"}
    assert body.calls == 1
    assert client.calls == 1
    original_signature = inspect.signature(original)
    governed_signature = inspect.signature(governed)
    assert tuple(governed_signature.parameters) == tuple(original_signature.parameters)
    assert (
        governed_signature.parameters["query"].kind is original_signature.parameters["query"].kind
    )
    assert governed_signature.parameters["query"].default is inspect.Parameter.empty
    assert governed_signature.parameters["query"].annotation is str
    assert governed_signature.return_annotation is str


def test_cell_plain_callable_async_with_args_schema_preserves_interface_and_invokes_once() -> None:
    body = Body()
    original = async_callable_with_schema(body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == "adelete_row"
    assert governed.args_schema is not Args
    assert governed.args_schema.model_json_schema() == Args.model_json_schema()
    assert governed.args_schema.model_config == Args.model_config
    assert (
        governed.args_schema.model_validate({"table": "invoices", "row": 3}).model_dump()
        == Args(table="invoices", row=3).model_dump()
    )
    assert inspect.iscoroutinefunction(governed)
    assert asyncio.run(governed(table="invoices", row=3)) == "body-result"
    assert body.calls == 1
    assert client.calls == 1


def test_cell_plain_callable_async_without_args_schema_preserves_interface_and_invokes_once() -> (
    None
):
    body = Body()
    original = async_callable_without_schema(body)
    client = CountingClient()

    governed = wrap(original, client=client)

    assert governed.name == "asearch"
    assert governed.description == original.__doc__
    assert governed.args_schema is None
    assert inspect.iscoroutinefunction(governed)
    assert asyncio.run(governed("cats")) == "body-result"
    assert body.calls == 1
    assert client.calls == 1


# --------------------------------------------------------------------------- #
# R2 -- wrapping mutates neither the tool, its callables, nor the container.
# --------------------------------------------------------------------------- #


def test_wrapping_leaves_the_original_sync_tool_and_its_func_ungoverned() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    # Second names, bound BEFORE wrapping: identity assertions alone would not
    # notice a wrapper that rebound ``.func`` to a governed function.
    tool_reference = original
    func_reference = original.func
    client = CountingClient(verdict=DENY)

    governed = wrap(original, client=client)

    assert governed is not original
    assert original.func is func_reference
    assert original.coroutine is None
    # Both second references still execute the ungoverned body...
    assert func_reference(table="t", row=1) == "body-result"
    assert tool_reference.invoke({"table": "t", "row": 2}) == "body-result"
    assert body.calls == 2
    # ...and the decision client was never consulted through either of them,
    # which is what proves they reach the original body and not a governed one.
    assert client.calls == 0


def test_wrapping_leaves_the_original_async_tool_and_its_coroutine_ungoverned() -> None:
    body = Body()
    original = async_tool_with_schema(body)
    tool_reference = original
    coroutine_reference = original.coroutine
    client = CountingClient(verdict=DENY)

    wrap(original, client=client)

    assert original.coroutine is coroutine_reference
    assert original.func is None
    assert asyncio.run(coroutine_reference(table="t", row=1)) == "body-result"
    assert asyncio.run(tool_reference.ainvoke({"table": "t", "row": 2})) == "body-result"
    assert body.calls == 2
    assert client.calls == 0


def test_wrapping_leaves_the_original_plain_callable_ungoverned() -> None:
    body = Body()
    original = sync_callable_without_schema(body)
    callable_reference = original
    client = CountingClient(verdict=DENY)

    governed = wrap(original, client=client)

    assert governed is not original
    assert callable_reference("cats") == "body-result"
    assert body.calls == 1
    assert client.calls == 0


def test_wrapping_copies_the_supplied_container_instead_of_governing_it_in_place() -> None:
    body = Body()
    first = sync_tool_with_schema(body)
    second = sync_callable_without_schema(body)
    supplied = [first, second]

    governed = govern_tools(supplied, context=THREADED, client=CountingClient())

    assert governed is not supplied
    assert supplied == [first, second]
    assert governed[0] is not first
    assert governed[1] is not second


# --------------------------------------------------------------------------- #
# R4 / R5 / R6 -- the three outcomes, counted at the body.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("builder", [sync_tool_with_schema, sync_callable_with_schema])
def test_denied_sync_call_raises_policy_violation_and_the_body_runs_zero_times(
    builder: Any,
) -> None:
    body = Body()
    client = CountingClient(verdict=DENY)
    governed = wrap(builder(body), client=client)

    with pytest.raises(PolicyViolation):
        _invoke(governed, {"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 1


@pytest.mark.parametrize("builder", [async_tool_with_schema, async_callable_with_schema])
def test_denied_async_call_raises_policy_violation_and_the_body_runs_zero_times(
    builder: Any,
) -> None:
    body = Body()
    client = CountingClient(verdict=DENY)
    governed = wrap(builder(body), client=client)

    with pytest.raises(PolicyViolation):
        asyncio.run(_ainvoke(governed, {"table": "t", "row": 1}))

    assert body.calls == 0
    assert client.calls == 1


@pytest.mark.parametrize("builder", [sync_tool_with_schema, sync_callable_with_schema])
def test_approval_suspends_the_sync_call_before_the_body_with_a_versioned_payload(
    builder: Any,
) -> None:
    body = Body()
    client = CountingClient(verdict=APPROVE)
    pause = RecordingInterrupt()
    governed = wrap(builder(body), client=client, interrupt=pause)

    with pytest.raises(Suspended):
        _invoke(governed, {"table": "t", "row": 1})

    assert body.calls == 0
    assert len(pause.payloads) == 1
    payload = pause.payloads[0]
    assert payload["version"] == 1
    assert payload["kind"] == "tool_approval"
    assert payload["approval_ref"] == "approval-7"
    # The payload is written into graph state that outlives the run, so it has to
    # survive a checkpoint's serialization round trip byte-identically.
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize("builder", [async_tool_with_schema, async_callable_with_schema])
def test_approval_suspends_the_async_call_before_the_body_with_a_versioned_payload(
    builder: Any,
) -> None:
    body = Body()
    client = CountingClient(verdict=APPROVE)
    pause = RecordingInterrupt()
    governed = wrap(builder(body), client=client, interrupt=pause)

    with pytest.raises(Suspended):
        asyncio.run(_ainvoke(governed, {"table": "t", "row": 1}))

    assert body.calls == 0
    assert len(pause.payloads) == 1
    assert json.loads(json.dumps(pause.payloads[0])) == pause.payloads[0]


def test_allowed_call_reaches_the_body_exactly_once_per_invocation() -> None:
    body = Body()
    client = CountingClient()
    governed = wrap(sync_tool_with_schema(body), client=client)

    # Three invocations in a row: the identity is re-derived from the live tool
    # on every call, so a non-deterministic derivation would allow the first and
    # refuse every one after it.
    for row in (1, 2, 3):
        assert governed.invoke({"table": "t", "row": row}) == "body-result"

    assert body.calls == 3
    assert client.calls == 3


def test_an_exception_from_an_allowed_body_propagates_unchanged_and_is_never_retried() -> None:
    body = Body()
    client = CountingClient()
    governed = wrap(AsyncSchemalessTool(body=body), client=client)

    # An async-only tool refuses the sync path. The wrapper decides FIRST and
    # then propagates that refusal untouched: no swallow, no retry, no
    # substituted result.
    with pytest.raises(NotImplementedError):
        governed.invoke("hello")

    assert client.calls == 1
    assert body.calls == 0


# --------------------------------------------------------------------------- #
# Fail-closed: no context, no classification, no verdict.
# --------------------------------------------------------------------------- #


def test_a_tool_list_wrapped_without_a_context_refuses_every_call() -> None:
    body = Body()
    client = CountingClient()
    governed = govern_tools([sync_tool_with_schema(body)], client=client, side_effect=read_only)[0]

    with pytest.raises(GovernanceContextError):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 0


def test_a_context_provider_that_fails_refuses_the_call_instead_of_attributing_it() -> None:
    body = Body()
    client = CountingClient()

    def broken_provider() -> ToolGovernanceContext:
        """Fail the way a context source that lost its request scope would."""
        raise RuntimeError("no request scope")

    governed = wrap(sync_tool_with_schema(body), client=client, context=broken_provider)

    with pytest.raises(GovernanceContextError):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 0


def test_a_context_provider_is_consulted_per_call_rather_than_pinned_at_wrap_time() -> None:
    body = Body()
    client = CountingClient()
    contexts = [THREADED, THREADLESS]

    def provider() -> ToolGovernanceContext:
        """Return a different context on each call, as a per-run source would."""
        return contexts.pop(0)

    governed = wrap(sync_tool_with_schema(body), client=client, context=provider)

    assert governed.invoke({"table": "t", "row": 1}) == "body-result"
    assert governed.invoke({"table": "t", "row": 2}) == "body-result"
    assert contexts == []
    assert body.calls == 2


def test_an_unclassified_tool_is_denied_before_the_client_is_ever_consulted() -> None:
    body = Body()
    client = CountingClient()
    # No ``side_effect`` resolver: the tool stays UNKNOWN, which the default
    # policy refuses.
    governed = govern_tools([sync_tool_with_schema(body)], context=THREADED, client=client)[0]

    with pytest.raises(PolicyViolation):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 0


def test_the_named_opt_in_is_what_lets_an_unclassified_tool_through() -> None:
    body = Body()
    client = CountingClient()
    governed = govern_tools(
        [sync_tool_with_schema(body)],
        context=THREADED,
        client=client,
        unknown_side_effect=UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS,
    )[0]

    assert governed.invoke({"table": "t", "row": 1}) == "body-result"
    assert body.calls == 1


def test_a_classifier_returning_the_bare_string_does_not_classify_the_tool() -> None:
    body = Body()
    client = CountingClient()

    def bare_string(_target: object) -> Any:
        """Return the StrEnum's *value*, which equals the member but is not one."""
        return SideEffectClass.READ_ONLY.value

    governed = govern_tools(
        [sync_tool_with_schema(body)], context=THREADED, client=client, side_effect=bare_string
    )[0]

    # ``SideEffectClass`` is a StrEnum, so ``"read_only" == SideEffectClass.READ_ONLY``.
    # The exact-type gate is what stops a classification nobody made from looking
    # like one somebody did -- so the tool stays unknown and is denied.
    with pytest.raises(PolicyViolation):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 0


def test_a_classifier_that_raises_refuses_the_wrap() -> None:
    body = Body()

    def broken(_target: object) -> SideEffectClass:
        """Fail the way a classifier that lost its registry would."""
        raise RuntimeError("classifier down")

    with pytest.raises(ToolGovernanceError, match="metadata resolver failed"):
        govern_tools([sync_tool_with_schema(body)], context=THREADED, side_effect=broken)
    assert body.calls == 0


def test_no_decision_client_denies_every_call() -> None:
    body = Body()
    governed = govern_tools([sync_tool_with_schema(body)], context=THREADED, side_effect=read_only)[
        0
    ]

    with pytest.raises(PolicyViolation):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0


# --------------------------------------------------------------------------- #
# Hostile inputs: subtypes, moving identities, raising properties.
# --------------------------------------------------------------------------- #


def test_a_tool_whose_name_is_a_str_subclass_is_refused_at_wrapping() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    object.__setattr__(original, "name", HostileStr("delete_row"))

    with pytest.raises(UnstableToolIdentityError):
        govern_tools([original], context=THREADED)

    assert body.calls == 0


def test_a_tool_whose_description_is_a_str_subclass_never_carries_it_into_the_wrapper() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    object.__setattr__(original, "description", HostileStr("Delete one row."))

    governed = wrap(original, client=CountingClient())

    assert governed.description == ""
    assert CONTENT_SENTINEL not in f"{governed.description}"


def test_a_tool_whose_identity_moves_after_wrapping_refuses_the_call() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    client = CountingClient()
    governed = wrap(original, client=client)

    object.__setattr__(original, "name", "something_else")

    with pytest.raises(UnstableToolIdentityError):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 0


def test_a_tool_whose_declared_schema_moves_after_wrapping_refuses_the_call() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    client = CountingClient()
    governed = wrap(original, client=client)

    class Widened(BaseModel):
        """A schema with a field the governed identity never saw."""

        table: str
        row: int
        force: bool = False

    object.__setattr__(original, "args_schema", Widened)

    with pytest.raises(UnstableToolIdentityError):
        governed.invoke({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 0


def test_a_tool_whose_args_schema_property_raises_is_still_governed_as_schemaless() -> None:
    body = Body()

    class Exploding:
        """A callable tool whose declared schema cannot be read at all."""

        name = "exploding"
        description = "Its schema raises."

        @property
        def args_schema(self) -> Any:
            """Raise, as a misconfigured lazily built schema would."""
            raise RuntimeError("schema unavailable")

        def __call__(self, query: str) -> str:
            """Count this execution."""
            return body.run(query=query)

    governed = wrap(Exploding(), client=CountingClient())

    # A raising property means "this tool declared nothing", never a traceback
    # out of the wrapping and never an ungoverned tool in the returned list.
    assert governed.args_schema is None
    assert governed.name == "exploding"
    assert governed("cats") == "body-result"
    assert body.calls == 1


def test_a_tool_whose_declared_schema_cannot_build_a_wrapper_is_refused() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    object.__setattr__(original, "args_schema", object())

    # Refusing is the only outcome that cannot leave an ungoverned tool in the
    # list the caller is about to hand to a graph.
    with pytest.raises(ToolGovernanceError):
        govern_tools([original], context=THREADED)


def test_a_hostile_container_of_tools_cannot_smuggle_a_non_tool_into_the_governed_list() -> None:
    body = Body()
    hostile = HostileList([sync_tool_with_schema(body)])

    # The list's ``__iter__`` yields a bare string instead of the tool it holds.
    with pytest.raises(ToolGovernanceError):
        govern_tools(hostile, context=THREADED)


def test_a_hostile_mapping_argument_is_refused_before_the_client_is_consulted() -> None:
    body = Body()
    client = CountingClient()
    governed = wrap(sync_callable_with_schema(body), client=client)

    with pytest.raises(ToolGovernanceError):
        governed(table="t", row=HostileDict({"nested": 1}))

    assert body.calls == 0
    assert client.calls == 0


def test_an_item_that_is_neither_a_tool_nor_callable_is_refused() -> None:
    with pytest.raises(ToolGovernanceError):
        govern_tools([object()], context=THREADED)


def test_a_non_iterable_tool_list_is_refused_as_a_governance_failure() -> None:
    with pytest.raises(ToolGovernanceError):
        govern_tools(object(), context=THREADED)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The production call shape: a ToolCall, as ``ToolNode`` sends it.
# --------------------------------------------------------------------------- #


def test_a_tool_call_round_trips_through_the_wrapper_identically() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    governed = wrap(sync_tool_with_schema(body), client=CountingClient())
    call = {"name": "delete_row", "args": {"table": "t", "row": 1}, "id": "c1", "type": "tool_call"}

    # ``ToolNode`` invokes with a ToolCall, not a bare argument dict, and expects
    # a ``ToolMessage`` back carrying the call id.
    assert governed.invoke(call) == original.invoke(call)
    assert body.calls == 2


def test_return_direct_survives_the_wrapping_because_the_agent_loop_reads_it() -> None:
    body = Body()

    def delete_row(table: str, row: int) -> str:
        """Delete one row."""
        return body.run(table=table, row=row)

    original = StructuredTool.from_function(
        func=delete_row, name="delete_row", description="d", args_schema=Args, return_direct=True
    )

    governed = wrap(original, client=CountingClient())

    # The loop reads this off the tool object it was handed -- the wrapper -- and
    # never off the delegate it cannot see, so a default here changes control flow.
    assert governed.return_direct is True


def test_content_and_artifact_survives_the_wrapping_and_the_artifact_is_not_dropped() -> None:
    def measure(table: str) -> Any:
        """Return content plus an artifact."""
        return "content", {"rows": 3}

    class OneArg(BaseModel):
        """A single-field schema."""

        table: str

    original = StructuredTool.from_function(
        func=measure,
        name="measure",
        description="d",
        args_schema=OneArg,
        response_format="content_and_artifact",
    )
    governed = wrap(original, client=CountingClient())
    call = {"name": "measure", "args": {"table": "t"}, "id": "c1", "type": "tool_call"}

    # Direct frozen-body execution returns the two-tuple to the governed wrapper,
    # so the outer (and only) BaseTool layer owns artifact formatting.
    assert governed.response_format == "content_and_artifact"
    assert governed.invoke(call) == original.invoke(call)
    assert governed.invoke(call).artifact == {"rows": 3}


def test_the_obsolete_call_id_carrier_name_is_an_ordinary_argument() -> None:
    class Colliding(BaseModel):
        """A schema loose enough to pass an extra argument through."""

        model_config = ConfigDict(extra="allow")

        table: str = "t"

    def echo(**kwargs: Any) -> Any:
        """Echo."""
        return kwargs

    original = StructuredTool.from_function(
        func=echo, name="colliding", description="d", args_schema=Colliding
    )
    governed = wrap(original, client=CountingClient())

    # Direct execution has no private call-id carrier, so this name is no longer
    # reserved and reaches the body exactly as policy saw it.
    assert governed.invoke({"table": "t", "__zeroth_tool_call_id__": "ordinary"}) == {
        "table": "t",
        "__zeroth_tool_call_id__": "ordinary",
    }


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
@pytest.mark.parametrize(
    ("name", "call_id"),
    [("other", "call-valid"), ("lookup", "")],
    ids=("wrong-name", "blank-id"),
)
def test_only_a_valid_full_tool_call_can_seed_trusted_identity(
    async_call: bool, name: str, call_id: str
) -> None:
    observed: list[str] = []

    def lookup(query: str) -> str:
        observed.append(query)
        return query

    async def alookup(query: str) -> str:
        observed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
    )
    client = CountingClient()
    governed = wrap(original, client=client)
    call = {
        "name": name,
        "args": {"query": "forged"},
        "id": call_id,
        "type": "tool_call",
    }

    with pytest.raises(ToolGovernanceError, match="full tool call"):
        if async_call:
            asyncio.run(governed.ainvoke(call))
        else:
            governed.invoke(call)

    assert client.calls == 0
    assert observed == []


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_direct_run_tool_call_id_cannot_seed_trusted_identity(async_call: bool) -> None:
    observed: list[str] = []

    def lookup(query: str) -> str:
        observed.append(query)
        return query

    async def alookup(query: str) -> str:
        observed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
    )
    client = CountingClient()
    governed = wrap(original, client=client)

    with pytest.raises(ToolGovernanceError, match="full tool call"):
        if async_call:
            asyncio.run(governed.arun({"query": "forged"}, tool_call_id="call-forged"))
        else:
            governed.run({"query": "forged"}, tool_call_id="call-forged")

    assert client.calls == 0
    assert observed == []


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_injected_tool_call_id_without_a_trusted_outer_call_is_refused(
    monkeypatch: Any,
    async_call: bool,
) -> None:
    observed: list[str] = []

    class InjectedArgs(BaseModel):
        query: str
        tool_call_id: Annotated[str, InjectedToolCallId]

    def lookup(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        observed.append(f"{query}:{tool_call_id}")
        return query

    async def alookup(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        observed.append(f"{query}:{tool_call_id}")
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
        args_schema=InjectedArgs,
    )
    client = CountingClient()
    governed = wrap(original, client=client)
    native_parse = BaseTool._parse_input

    def injected_parse(self: Any, tool_input: Any, tool_call_id: Any) -> Any:
        if tool_call_id is None:
            return {**tool_input, "tool_call_id": "call-forged"}
        return native_parse(self, tool_input, tool_call_id)

    monkeypatch.setattr(GovernedTool, "_parse_input", injected_parse)

    with pytest.raises(ToolGovernanceError, match="full tool call"):
        if async_call:
            asyncio.run(governed.ainvoke({"query": "forged"}))
        else:
            governed.invoke({"query": "forged"})

    assert client.calls == 0
    assert observed == []


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_full_tool_call_identity_does_not_leak_to_the_next_ordinary_call(
    async_call: bool,
) -> None:
    observed: list[str] = []

    def lookup(query: str) -> str:
        observed.append(query)
        return query

    async def alookup(query: str) -> str:
        observed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
    )
    client = CountingClient()
    governed = wrap(original, client=client)
    call = {
        "name": "lookup",
        "args": {"query": "first"},
        "id": "call-first",
        "type": "tool_call",
    }

    if async_call:
        asyncio.run(governed.ainvoke(call))
        asyncio.run(governed.ainvoke({"query": "second"}))
    else:
        governed.invoke(call)
        governed.invoke({"query": "second"})

    assert [action.tool_call_id for action in client.seen] == ["call-first", None]
    assert observed == ["first", "second"]


def test_reentrant_callback_cannot_move_outer_identity_to_an_ordinary_call() -> None:
    observed: list[str] = []

    def lookup(query: str) -> str:
        observed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        name="lookup",
        description="Lookup.",
    )
    client = CountingClient()
    governed = wrap(original, client=client)

    class ReentrantHandler(BaseCallbackHandler):
        entered = False

        def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:
            del serialized, input_str, kwargs
            if not self.entered:
                self.entered = True
                governed.invoke({"query": "nested"})

    governed.invoke(
        {
            "name": "lookup",
            "args": {"query": "outer"},
            "id": "call-outer",
            "type": "tool_call",
        },
        config={"callbacks": [ReentrantHandler()]},
    )

    assert [action.tool_call_id for action in client.seen] == [None, "call-outer"]
    assert observed == ["nested", "outer"]


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_full_tool_call_identity_resets_after_an_error(async_call: bool) -> None:
    observed: list[str] = []

    def lookup(query: str) -> str:
        if query == "fail":
            raise RuntimeError("failed")
        observed.append(query)
        return query

    async def alookup(query: str) -> str:
        if query == "fail":
            raise RuntimeError("failed")
        observed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
    )
    client = CountingClient()
    governed = wrap(original, client=client)
    call = {
        "name": "lookup",
        "args": {"query": "fail"},
        "id": "call-failed",
        "type": "tool_call",
    }

    with pytest.raises(RuntimeError, match="failed"):
        if async_call:
            asyncio.run(governed.ainvoke(call))
        else:
            governed.invoke(call)
    if async_call:
        asyncio.run(governed.ainvoke({"query": "next"}))
    else:
        governed.invoke({"query": "next"})

    assert [action.tool_call_id for action in client.seen] == ["call-failed", None]
    assert observed == ["next"]


def test_full_tool_call_identity_resets_after_async_cancellation() -> None:
    observed: list[str] = []

    def lookup(query: str) -> str:
        observed.append(query)
        return query

    started = asyncio.Event()

    async def alookup(query: str) -> str:
        if query == "cancel":
            started.set()
            await asyncio.Event().wait()
        observed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
    )
    client = CountingClient()
    governed = wrap(original, client=client)

    async def scenario() -> None:
        task = asyncio.create_task(
            governed.ainvoke(
                {
                    "name": "lookup",
                    "args": {"query": "cancel"},
                    "id": "call-cancelled",
                    "type": "tool_call",
                }
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await governed.ainvoke({"query": "next"})

    asyncio.run(scenario())

    assert [action.tool_call_id for action in client.seen] == ["call-cancelled", None]
    assert observed == ["next"]


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_injected_tool_call_id_must_match_the_trusted_outer_id(
    monkeypatch: Any, async_call: bool
) -> None:
    observed: list[str] = []

    class InjectedArgs(BaseModel):
        query: str
        tool_call_id: Annotated[str, InjectedToolCallId]

    def lookup(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        observed.append(f"{query}:{tool_call_id}")
        return query

    async def alookup(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        observed.append(f"{query}:{tool_call_id}")
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Lookup.",
        args_schema=InjectedArgs,
    )
    client = CountingClient()
    governed = wrap(original, client=client)
    native_parse = BaseTool._parse_input

    def conflicting_parse(self: Any, tool_input: Any, tool_call_id: Any) -> Any:
        parsed = native_parse(self, tool_input, tool_call_id)
        return {**parsed, "tool_call_id": "call-forged"}

    monkeypatch.setattr(GovernedTool, "_parse_input", conflicting_parse)
    call = {
        "name": "lookup",
        "args": {"query": "first"},
        "id": "call-outer",
        "type": "tool_call",
    }

    with pytest.raises(ToolGovernanceError, match="tool-call identity"):
        if async_call:
            asyncio.run(governed.ainvoke(call))
        else:
            governed.invoke(call)

    assert client.calls == 0
    assert observed == []


def test_tags_and_metadata_a_caller_reads_off_the_tool_survive_the_wrapping() -> None:
    body = Body()

    def delete_row(table: str, row: int) -> str:
        """Delete one row."""
        return body.run(table=table, row=row)

    original = StructuredTool.from_function(
        func=delete_row,
        name="delete_row",
        description="d",
        args_schema=Args,
        tags=["billing"],
        metadata={"team": "payments"},
    )

    governed = wrap(original, client=CountingClient())

    assert governed.tags == ["billing"]
    assert governed.metadata == {"team": "payments"}


def test_the_outer_wrapper_owns_tool_error_handling() -> None:
    def explode(table: str) -> str:
        """Fail the way a tool whose backend is down does."""
        raise ToolException("backend down")

    class OneArg(BaseModel):
        """A single-field schema."""

        table: str

    original = StructuredTool.from_function(
        func=explode,
        name="explode",
        description="d",
        args_schema=OneArg,
        handle_tool_error="handled",
    )
    governed = wrap(original, client=CountingClient())

    # The frozen body now runs directly. The governed wrapper is the only
    # BaseTool layer, so it is also the only layer that handles the exception.
    assert governed.handle_tool_error == "handled"
    assert governed.invoke({"table": "t"}) == original.invoke({"table": "t"}) == "handled"


class ErrorArgs(BaseModel):
    """The single argument shared by direct-execution error tests."""

    table: str


@pytest.mark.parametrize("asynchronous", (False, True), ids=("sync", "async"))
@pytest.mark.parametrize("handler_kind", ("boolean", "callable"))
def test_handle_tool_error_runs_exactly_once(asynchronous: bool, handler_kind: str) -> None:
    """Boolean and callable handlers are applied once by the outer tool."""
    body_calls: list[str] = []
    handler_calls: list[str] = []

    def handler(error: ToolException) -> str:
        handler_calls.append(str(error))
        return f"handled:{error}"

    flag: Any = True if handler_kind == "boolean" else handler

    if asynchronous:

        async def explode(table: str) -> str:
            """Fail natively asynchronously."""
            body_calls.append(table)
            raise ToolException("backend down")

        original = StructuredTool.from_function(
            coroutine=explode,
            name="explode",
            description="d",
            args_schema=ErrorArgs,
            handle_tool_error=flag,
        )
    else:

        def explode(table: str) -> str:
            """Fail synchronously."""
            body_calls.append(table)
            raise ToolException("backend down")

        original = StructuredTool.from_function(
            func=explode,
            name="explode",
            description="d",
            args_schema=ErrorArgs,
            handle_tool_error=flag,
        )

    governed = wrap(original, client=CountingClient())
    result = (
        asyncio.run(governed.ainvoke({"table": "t"}))
        if asynchronous
        else governed.invoke({"table": "t"})
    )
    assert result == ("backend down" if handler_kind == "boolean" else "handled:backend down")
    assert body_calls == ["t"]
    assert handler_calls == ([] if handler_kind == "boolean" else ["backend down"])


@pytest.mark.parametrize("asynchronous", (False, True), ids=("sync", "async"))
def test_handle_tool_error_does_not_swallow_ordinary_exceptions(asynchronous: bool) -> None:
    """Only ToolException enters BaseTool's configured error handler."""
    handler_calls: list[str] = []

    def handler(error: ToolException) -> str:
        handler_calls.append(str(error))
        return "handled"

    if asynchronous:

        async def explode(table: str) -> str:
            """Raise outside the handled exception type."""
            raise ValueError(table)

        original = StructuredTool.from_function(
            coroutine=explode,
            name="explode",
            description="d",
            args_schema=ErrorArgs,
            handle_tool_error=handler,
        )
    else:

        def explode(table: str) -> str:
            """Raise outside the handled exception type."""
            raise ValueError(table)

        original = StructuredTool.from_function(
            func=explode,
            name="explode",
            description="d",
            args_schema=ErrorArgs,
            handle_tool_error=handler,
        )

    governed = wrap(original, client=CountingClient())
    with pytest.raises(ValueError, match="t"):
        if asynchronous:
            asyncio.run(governed.ainvoke({"table": "t"}))
        else:
            governed.invoke({"table": "t"})
    assert handler_calls == []


# --------------------------------------------------------------------------- #
# One validation path: the arguments policy authorized are the arguments the
# body receives.
# --------------------------------------------------------------------------- #


def drifting_schema(passes: list[int]) -> type[BaseModel]:
    """Build a schema whose validator answers differently on its second pass.

    Not pathological: any validator that reads the clock, consumes a nonce,
    decrypts with a rotating key or normalizes against mutable state behaves this
    way. It is the general shape of "validation is not idempotent", written down
    so the property can be asserted rather than assumed. The counter lives in the
    caller's list rather than on the class so that two tests using this schema
    cannot see each other's passes.

    Args:
        passes: Appended to once per validation, so the test can count them.

    Returns:
        A single-field schema.
    """

    class Drifting(BaseModel):
        """A single-field schema whose validator is stateful."""

        query: str

        @pydantic.field_validator("query")
        @classmethod
        def _drift(cls, _value: str) -> str:
            passes.append(1)
            return "safe" if len(passes) == 1 else "danger"

    return Drifting


def test_a_stateful_validator_cannot_make_the_body_run_arguments_policy_never_saw() -> None:
    passes: list[int] = []
    ran: list[str] = []

    def search(query: str) -> str:
        """Record what the body was actually handed."""
        ran.append(query)
        return "body-result"

    original = StructuredTool.from_function(
        func=search, name="search", description="d", args_schema=drifting_schema(passes)
    )
    client = CountingClient()
    governed = wrap(original, client=client)

    assert governed.invoke({"query": "original"}) == "body-result"

    # The authorization property, stated as the equality it is: the values the
    # decision was made about and the values the body received are the same. A
    # second validation -- which invoking the delegate itself performs -- makes
    # policy see ``safe`` while the body runs ``danger``.
    [decided] = client.seen
    assert dict(decided.arguments) == {"query": "safe"}
    assert ran == ["safe"]
    assert len(passes) == 1


def test_a_stateful_validator_cannot_slip_past_the_async_surface_either() -> None:
    passes: list[int] = []
    ran: list[str] = []

    async def asearch(query: str) -> str:
        """Record what the body was actually handed."""
        ran.append(query)
        return "body-result"

    original = StructuredTool.from_function(
        coroutine=asearch, name="asearch", description="d", args_schema=drifting_schema(passes)
    )
    client = CountingClient()
    governed = wrap(original, client=client)

    assert asyncio.run(governed.ainvoke({"query": "original"})) == "body-result"

    [decided] = client.seen
    assert dict(decided.arguments) == {"query": "safe"}
    assert ran == ["safe"]
    assert len(passes) == 1


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
@pytest.mark.parametrize("policy", ["deny-danger", "allow-exact"])
def test_unedited_schemaless_base_tool_materializes_defaults_before_policy(
    async_call: bool, policy: str
) -> None:
    effects: list[str] = []
    original: BaseTool = (
        AsyncDefaultingSchemalessTool(effects=effects)
        if async_call
        else DefaultingSchemalessTool(effects=effects)
    )

    @dataclasses.dataclass
    class DefaultAwarePolicy:
        seen: list[ToolAction] = dataclasses.field(default_factory=list)

        def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
            del context
            self.seen.append(action)
            visible = dict(action.arguments) == {"path": "/danger"}
            if policy == "deny-danger":
                return DENY if visible else ALLOW
            return ALLOW if visible else DENY

    client = DefaultAwarePolicy()
    governed = wrap(original, client=client)  # type: ignore[arg-type]

    def invoke() -> Any:
        return asyncio.run(governed.ainvoke({})) if async_call else governed.invoke({})

    if policy == "deny-danger":
        with pytest.raises(PolicyViolation):
            invoke()
        assert effects == []
    else:
        assert invoke() == "/danger"
        assert effects == ["/danger"]

    assert [dict(action.arguments) for action in client.seen] == [{"path": "/danger"}]
    if effects:
        assert client.seen[0].arguments["path"] == effects[0]


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_approved_callable_edit_materializes_defaults_before_fresh_policy(
    tmp_path: Any, async_call: bool
) -> None:
    effects: list[str] = []

    if async_call:

        async def remove(path: str = "/danger") -> None:
            effects.append(path)

    else:

        def remove(path: str = "/danger") -> None:
            effects.append(path)

    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    client = DefaultDenyReplayClient()
    interrupt = ApprovalReplayInterrupt()
    [governed] = govern_tools(
        [remove],
        context=THREADED,
        client=client,
        side_effect=read_only,
        interrupt=interrupt,
        approval_lifecycle=repository,
    )

    def invoke() -> Any:
        return asyncio.run(governed()) if async_call else governed()

    with pytest.raises(Suspended):
        invoke()
    repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE, {})
    repository.decide(resolution)
    claimed = repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    interrupt.delivery = {**resolution.to_payload(), "claim_token": claimed.claim_token}

    with pytest.raises(PolicyViolation):
        invoke()

    assert dict(client.seen[-1].arguments) == {"path": "/danger"}
    assert effects == []
    assert repository.get("approval-7").state is ApprovalState.RESOLVED


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
@pytest.mark.parametrize("fresh_policy", ["deny-danger", "allow-exact"])
def test_approved_schemaless_base_tool_edit_materializes_frozen_body_defaults(
    tmp_path: Any, async_call: bool, fresh_policy: str
) -> None:
    effects: list[str] = []
    original: BaseTool = (
        AsyncDefaultingSchemalessTool(effects=effects)
        if async_call
        else DefaultingSchemalessTool(effects=effects)
    )

    @dataclasses.dataclass
    class DefaultAwarePolicy:
        seen: list[ToolAction] = dataclasses.field(default_factory=list)

        def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
            del context
            self.seen.append(action)
            if len(self.seen) == 1:
                return APPROVE
            visible = dict(action.arguments) == {"path": "/danger"}
            if fresh_policy == "deny-danger":
                return DENY if visible else ALLOW
            return ALLOW if visible else DENY

    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    client = DefaultAwarePolicy()
    interrupt = ApprovalReplayInterrupt()
    [governed] = govern_tools(
        [original],
        context=THREADED,
        client=client,
        side_effect=read_only,
        interrupt=interrupt,
        approval_lifecycle=repository,
    )

    def invoke() -> Any:
        call = {"path": "/safe"}
        return asyncio.run(governed.ainvoke(call)) if async_call else governed.invoke(call)

    with pytest.raises(Suspended):
        invoke()
    repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE, {})
    repository.decide(resolution)
    claimed = repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    interrupt.delivery = {**resolution.to_payload(), "claim_token": claimed.claim_token}

    if fresh_policy == "deny-danger":
        with pytest.raises(PolicyViolation):
            invoke()
        assert effects == []
    else:
        assert invoke() == "/danger"
        assert effects == ["/danger"]

    assert [dict(action.arguments) for action in client.seen] == [
        {"path": "/safe"},
        {"path": "/danger"},
    ]
    assert repository.get("approval-7").state is ApprovalState.RESOLVED


@pytest.mark.parametrize("async_call", [False, True], ids=("sync", "async"))
def test_approved_callable_kwargs_edit_is_bound_once_for_policy_and_execution(
    tmp_path: Any, async_call: bool
) -> None:
    unsafe = "/danger"
    persisted = {"path": "/safe"}
    body_arguments: list[dict[str, Any]] = []
    effects: list[str] = []

    if async_call:

        async def remove(**kwargs: Any) -> None:
            body_arguments.append(dict(kwargs))
            effects.append(kwargs.get("path", unsafe))

    else:

        def remove(**kwargs: Any) -> None:
            body_arguments.append(dict(kwargs))
            effects.append(kwargs.get("path", unsafe))

    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    client = ApprovalReplayClient()
    interrupt = ApprovalReplayInterrupt()
    [governed] = govern_tools(
        [remove],
        context=THREADED,
        client=client,
        side_effect=read_only,
        interrupt=interrupt,
        approval_lifecycle=repository,
    )

    def invoke() -> Any:
        return asyncio.run(governed()) if async_call else governed()

    with pytest.raises(Suspended):
        invoke()
    repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE, persisted)
    repository.decide(resolution)
    claimed = repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    interrupt.delivery = {**resolution.to_payload(), "claim_token": claimed.claim_token}

    invoke()

    stored = repository.get("approval-7")
    assert stored.resolution is not None and stored.resolution.arguments == persisted
    assert dict(client.seen[-1].arguments) == {"kwargs": persisted}
    assert client.seen[-1].arguments["kwargs"] == body_arguments[0] == persisted
    assert effects == ["/safe"]
    assert unsafe not in effects
    assert stored.state is ApprovalState.RESOLVED


def test_approved_base_tool_edit_uses_native_schema_coercion(tmp_path: Any) -> None:
    received: list[tuple[str, int]] = []

    def update_row(table: str, row: int) -> int:
        """Record the exact values that reached the tool body."""
        received.append((table, row))
        return row

    original = StructuredTool.from_function(
        func=update_row,
        name="update_row",
        description="Update one row.",
        args_schema=Args,
    )
    governed, repository, client, _interrupt = prepare_base_tool_approval_replay(
        tmp_path,
        original,
        {"table": "invoices", "row": 1},
        {"table": "invoices", "row": "42"},
    )

    assert governed.invoke({"table": "invoices", "row": 1}) == 42
    assert received == [("invoices", 42)]
    assert dict(client.seen[-1].arguments) == {"table": "invoices", "row": 42}
    assert repository.get("approval-7").state is ApprovalState.RESOLVED


def test_invalid_approved_base_tool_edit_fails_before_execution(tmp_path: Any) -> None:
    body = Body()
    governed, repository, _client, _interrupt = prepare_base_tool_approval_replay(
        tmp_path,
        sync_tool_with_schema(body),
        {"table": "invoices", "row": 1},
        {"table": "invoices", "row": "not-an-integer"},
    )

    with pytest.raises(pydantic.ValidationError):
        governed.invoke({"table": "invoices", "row": 1})

    assert body.calls == 0
    assert repository.get("approval-7").state is ApprovalState.ORPHANED


def test_approved_base_tool_edit_runs_field_validation_once_before_policy(
    tmp_path: Any,
) -> None:
    passes: list[str] = []
    received: list[str] = []

    class NormalizedQuery(BaseModel):
        query: str

        @pydantic.field_validator("query")
        @classmethod
        def normalize(cls, value: str) -> str:
            passes.append(value)
            return value.strip().lower()

    def search(query: str) -> str:
        """Record the schema-normalized query."""
        received.append(query)
        return query

    original = StructuredTool.from_function(
        func=search,
        name="search",
        description="Search.",
        args_schema=NormalizedQuery,
    )
    governed, repository, client, _interrupt = prepare_base_tool_approval_replay(
        tmp_path,
        original,
        {"query": "original"},
        {"query": "  SAFE  "},
    )

    assert governed.invoke({"query": "original"}) == "safe"
    assert passes == ["original", "original", "  SAFE  "]
    assert received == ["safe"]
    assert dict(client.seen[-1].arguments) == {"query": "safe"}
    assert repository.get("approval-7").state is ApprovalState.RESOLVED


def test_approved_base_tool_edit_cannot_replace_an_injected_argument(tmp_path: Any) -> None:
    calls: list[tuple[str, str]] = []

    class InjectedArgs(BaseModel):
        query: str
        tool_call_id: Annotated[str, InjectedToolCallId]

    def lookup(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        """Record the framework-injected call identity."""
        calls.append((query, tool_call_id))
        return query

    original = StructuredTool.from_function(
        func=lookup,
        name="lookup",
        description="Lookup.",
        args_schema=InjectedArgs,
    )
    call = {
        "name": "lookup",
        "args": {"query": "original"},
        "id": "call-original",
        "type": "tool_call",
    }
    governed, repository, _client, _interrupt = prepare_base_tool_approval_replay(
        tmp_path,
        original,
        call,
        {"query": "edited", "tool_call_id": "call-replacement"},
    )

    with pytest.raises(ToolGovernanceError, match="injected"):
        governed.invoke(call)

    assert calls == []
    assert repository.get("approval-7").state is ApprovalState.ORPHANED


def test_approved_base_tool_edit_preserves_the_injected_tool_call_id(tmp_path: Any) -> None:
    calls: list[tuple[str, str]] = []

    class InjectedArgs(BaseModel):
        query: str
        tool_call_id: Annotated[str, InjectedToolCallId]

    def lookup(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        """Record the framework-injected call identity."""
        calls.append((query, tool_call_id))
        return query

    original = StructuredTool.from_function(
        func=lookup,
        name="lookup",
        description="Lookup.",
        args_schema=InjectedArgs,
    )
    call = {
        "name": "lookup",
        "args": {"query": "original"},
        "id": "call-original",
        "type": "tool_call",
    }
    governed, repository, client, _interrupt = prepare_base_tool_approval_replay(
        tmp_path,
        original,
        call,
        {"query": "edited"},
    )

    governed.invoke(call)

    assert calls == [("edited", "call-original")]
    assert client.seen[-1].tool_call_id == "call-original"
    assert repository.get("approval-7").state is ApprovalState.RESOLVED


def test_approved_edit_validation_uses_the_pre_interrupt_schema_snapshot(
    tmp_path: Any,
) -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    governed, repository, _client, interrupt = prepare_base_tool_approval_replay(
        tmp_path,
        original,
        {"table": "invoices", "row": 1},
        {"table": "invoices", "row": "not-an-integer"},
    )

    class PermissiveArgs(BaseModel):
        table: str
        row: str

    interrupt.before_delivery = lambda: setattr(original, "args_schema", PermissiveArgs)
    with pytest.raises(pydantic.ValidationError):
        governed.invoke({"table": "invoices", "row": 1})

    assert body.calls == 0
    assert repository.get("approval-7").state is ApprovalState.ORPHANED


def test_running_the_delegate_unvalidated_does_not_strip_the_delegates_own_validation() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    governed = wrap(original, client=CountingClient())

    # The wrapper carries the delegate's schema, so it is the wrapper's parse
    # that validates -- and it still refuses a call the schema rejects, before
    # any decision is asked for.
    with pytest.raises(Exception, match="row"):
        governed.invoke({"table": "invoices", "row": "not-an-int"})
    assert body.calls == 0

    # And the original is untouched: pass-through execution is arranged on a
    # twin, so every other holder of this tool still validates.
    assert original.args_schema is Args
    assert original.invoke({"table": "invoices", "row": 3}) == "body-result"


# --------------------------------------------------------------------------- #
# Reviewed tool metadata is pinned for inventory and rechecked before live
# decisions, so changed resolver answers fail closed.
# --------------------------------------------------------------------------- #


def test_the_classification_is_pinned_for_inventory_and_live_calls() -> None:
    body = Body()
    client = CountingClient()
    live = {"class": SideEffectClass.READ_ONLY}
    governed = govern_tools(
        [sync_tool_with_schema(body)],
        context=THREADED,
        client=client,
        side_effect=lambda _target: live["class"],
    )[0]

    live["class"] = SideEffectClass.SIDE_EFFECTING
    with pytest.raises(ToolGovernanceError, match="metadata changed"):
        governed.invoke({"table": "invoices", "row": 3})

    assert governed.zeroth_binding.side_effect is SideEffectClass.READ_ONLY
    assert client.seen == []
    assert body.calls == 0


def test_the_contract_binding_is_pinned_for_inventory_and_live_calls() -> None:
    body = Body()
    client = CountingClient()
    live = {"ref": "contract:v1"}
    governed = govern_tools(
        [sync_tool_with_schema(body)],
        context=THREADED,
        client=client,
        side_effect=read_only,
        contract_ref=lambda _target: live["ref"],
    )[0]

    live["ref"] = "contract:v2"
    with pytest.raises(ToolGovernanceError, match="metadata changed"):
        governed.invoke({"table": "invoices", "row": 3})

    assert governed.zeroth_binding.contract_ref == "contract:v1"
    assert client.seen == []
    assert body.calls == 0


def test_the_async_surface_reuses_the_same_pinned_facts() -> None:
    body = Body()
    client = CountingClient()
    live = {"class": SideEffectClass.READ_ONLY, "ref": "contract:v1"}
    governed = govern_tools(
        [async_tool_with_schema(body)],
        context=THREADED,
        client=client,
        side_effect=lambda _target: live["class"],
        contract_ref=lambda _target: live["ref"],
    )[0]

    live["class"], live["ref"] = SideEffectClass.SIDE_EFFECTING, "contract:v2"
    with pytest.raises(ToolGovernanceError, match="metadata changed"):
        asyncio.run(governed.ainvoke({"table": "invoices", "row": 3}))

    assert governed.zeroth_binding.side_effect is SideEffectClass.READ_ONLY
    assert governed.zeroth_binding.contract_ref == "contract:v1"
    assert client.seen == []
    assert body.calls == 0


# --------------------------------------------------------------------------- #
# The audit seam, threaded through to the enforcement core.
# --------------------------------------------------------------------------- #


def test_a_denied_call_is_projected_through_the_audit_seam_with_the_tool_it_denied() -> None:
    body = Body()
    submitter = RecordingSubmitter()
    actor = ActorIdentity(
        subject="principal-1", auth_method=AuthMethod.API_KEY, tenant_id="tenant-a"
    )
    governed = wrap(
        sync_tool_with_schema(body),
        client=CountingClient(verdict=DENY),
        audit=submitter,
        actor=actor,
    )

    with pytest.raises(PolicyViolation):
        governed.invoke({"table": "invoices", "row": 1})

    # The wrapper threads five explicit seams into the enforcement core; a
    # dropped ``audit`` or ``actor`` key would compile and pass every other test
    # in this file while producing a governed call with no record.
    assert len(submitter.records) == 1
    record = submitter.records[0]
    assert record.status == "rejected"
    assert record.execution_metadata["decision"] == "deny"
    assert record.actor == actor
    assert [call.alias for call in record.tool_calls] == ["delete_row"]
    assert record.tool_calls[0].tool_ref == governed.zeroth_binding.identity.fingerprint
    assert body.calls == 0


# --------------------------------------------------------------------------- #
# Coverage and binding.
# --------------------------------------------------------------------------- #


def test_govern_tools_declares_partial_coverage_for_every_wrapper_it_returns() -> None:
    body = Body()
    governed = govern_tools(
        [sync_tool_with_schema(body), sync_callable_without_schema(body)],
        context=THREADED,
        side_effect=read_only,
    )

    for wrapper in governed:
        assert wrapper.zeroth_binding.coverage is InventoryCoverage.PARTIAL


def test_every_wrapper_carries_its_pinned_identity_and_static_metadata() -> None:
    body = Body()
    governed = wrap(
        sync_tool_with_schema(body),
        client=CountingClient(),
        contract_ref=lambda _target: "contract:records",
    )

    binding = governed.zeroth_binding
    assert binding.identity.name == "delete_row"
    assert len(binding.identity.fingerprint) == 64
    assert binding.contract_ref == "contract:records"
    assert binding.side_effect is SideEffectClass.READ_ONLY


def test_two_wrappings_of_the_same_tool_pin_the_same_fingerprint() -> None:
    body = Body()
    original = sync_tool_with_schema(body)

    first = wrap(original, client=CountingClient())
    second = wrap(original, client=CountingClient())

    assert first.zeroth_binding.identity == second.zeroth_binding.identity


def test_the_same_function_governed_as_a_tool_and_as_a_callable_is_not_one_identity() -> None:
    body = Body()
    as_tool = wrap(sync_tool_with_schema(body), client=CountingClient())
    as_callable = wrap(sync_callable_with_schema(body), client=CountingClient())

    assert as_tool.zeroth_binding.identity.name == as_callable.zeroth_binding.identity.name
    assert (
        as_tool.zeroth_binding.identity.fingerprint
        != as_callable.zeroth_binding.identity.fingerprint
    )


# --------------------------------------------------------------------------- #
# Entry-point coverage: every BaseTool door funnels through the guard.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", ["invoke", "run"])
def test_every_sync_base_tool_entry_point_is_governed(entry: str) -> None:
    body = Body()
    client = CountingClient(verdict=DENY)
    governed = wrap(sync_tool_with_schema(body), client=client)

    with pytest.raises(PolicyViolation):
        getattr(governed, entry)({"table": "t", "row": 1})

    assert body.calls == 0
    assert client.calls == 1


@pytest.mark.parametrize("entry", ["ainvoke", "arun"])
def test_every_async_base_tool_entry_point_is_governed(entry: str) -> None:
    body = Body()
    client = CountingClient(verdict=DENY)
    governed = wrap(async_tool_with_schema(body), client=client)

    with pytest.raises(PolicyViolation):
        asyncio.run(getattr(governed, entry)({"table": "t", "row": 1}))

    assert body.calls == 0
    assert client.calls == 1


def test_a_governed_base_tool_is_not_directly_callable_just_as_the_original_is_not() -> None:
    body = Body()
    original = sync_tool_with_schema(body)
    governed = wrap(original, client=CountingClient())

    assert not callable(original)
    assert not callable(governed)


def test_the_governed_wrapper_is_exported_from_the_package() -> None:
    import zeroth.integrations.langgraph as package

    assert "govern_tools" in package.__all__
    assert package.govern_tools is govern_tools


def test_importing_the_package_does_not_import_the_tool_surface_or_opentelemetry() -> None:
    """The tool surface is exported lazily, so the package stays install-safe.

    ``from langchain_core.tools import BaseTool`` pulls ``langsmith``, which
    imports the OpenTelemetry SDK at module scope. An eager export of
    ``govern_tools`` would therefore make ``import zeroth.integrations.langgraph``
    drag in OpenTelemetry -- which ships only in the optional ``otel`` extra --
    for every caller who only wanted ``govern_graph``. Checked in a clean
    subprocess so the result does not depend on what this session imported.
    """
    code = (
        "import sys, zeroth.integrations.langgraph as pkg; "
        "leaked = sorted(k for k in sys.modules "
        "if k == 'opentelemetry' or k.startswith('opentelemetry.')); "
        "assert not leaked, leaked; "
        "assert 'zeroth.integrations.langgraph._tool_wrappers' not in sys.modules; "
        "assert 'govern_tools' in pkg.__all__; "
        "assert callable(pkg.govern_tools)"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr


def _invoke(governed: Any, arguments: dict[str, Any]) -> Any:
    """Call a governed wrapper through whichever sync interface it exposes."""
    if isinstance(governed, BaseTool):
        return governed.invoke(arguments)
    return governed(**arguments)


async def _ainvoke(governed: Any, arguments: dict[str, Any]) -> Any:
    """Call a governed wrapper through whichever async interface it exposes."""
    if isinstance(governed, BaseTool):
        return await governed.ainvoke(arguments)
    return await governed(**arguments)


# --------------------------------------------------------------------------- #
# C2-1 -- the tool the policy authorized is the tool that executes, and nothing
# assigned after the wrapping can change which one that is.
# --------------------------------------------------------------------------- #


class Query(BaseModel):
    """A one-field schema for the entry-hook probes."""

    query: str


def substitute_tool(body: Body) -> StructuredTool:
    """Build a tool whose whole declared surface matches ``sync_tool_with_schema``'s.

    Same name, same description, same ``args_schema``, different body -- the
    substitution the fingerprint exists to catch, and the object an attacker
    would assign over a mutable delegate handle.
    """

    def delete_row(table: str, row: int) -> str:
        """Delete one row."""
        return body.run(table=table, row=row)

    return StructuredTool.from_function(
        func=delete_row, name="delete_row", description="Delete one row.", args_schema=Args
    )


def test_a_delegate_assigned_after_the_wrapping_cannot_be_the_one_that_executes() -> None:
    """The confused deputy: identity comes from the plan, execution must too.

    A publicly assignable second reference to the delegate is authorization
    laundering -- policy is asked about the fingerprint the plan pins and the
    body that runs is whatever was assigned afterwards.
    """
    original_body, evil_body = Body("original-result"), Body("evil-result")
    client = CountingClient()
    governed = wrap(sync_tool_with_schema(original_body), client=client)

    with pytest.raises(ToolGovernanceError, match="cannot be reassigned"):
        governed.zeroth_delegate = substitute_tool(evil_body)

    assert governed.invoke({"table": "invoices", "row": 3}) == "original-result"
    assert original_body.calls == 1
    assert evil_body.calls == 0
    [decided] = client.seen
    assert decided.identity == governed.zeroth_binding.identity


def test_neither_the_plan_nor_the_binding_can_be_replaced_after_the_wrapping() -> None:
    original_body, evil_body = Body("original-result"), Body("evil-result")
    governed = wrap(sync_tool_with_schema(original_body), client=CountingClient())
    substitute = wrap(substitute_tool(evil_body), client=CountingClient())

    for name in ("_zeroth_plan", "zeroth_plan", "zeroth_binding", "zeroth_delegate"):
        with pytest.raises(ToolGovernanceError, match="cannot be reassigned"):
            setattr(governed, name, substitute._zeroth_plan)

    assert governed.invoke({"table": "invoices", "row": 3}) == "original-result"
    assert evil_body.calls == 0
    assert governed.zeroth_binding.identity != substitute.zeroth_binding.identity


def test_a_delegate_assigned_after_the_wrapping_cannot_reach_the_async_surface_either() -> None:
    original_body, evil_body = Body("original-result"), Body("evil-result")
    governed = wrap(async_tool_with_schema(original_body), client=CountingClient())

    with pytest.raises(ToolGovernanceError, match="cannot be reassigned"):
        governed.zeroth_delegate = substitute_tool(evil_body)

    assert asyncio.run(governed.ainvoke({"table": "invoices", "row": 3})) == "original-result"
    assert original_body.calls == 1
    assert evil_body.calls == 0


def test_a_governed_callable_publishes_no_handle_to_the_target_or_the_plan() -> None:
    """A function object cannot refuse an attribute, so it must publish none.

    The target and the plan are closed over by the governed function and read
    from nowhere else, so an attribute an attacker adds is inert rather than
    load-bearing.
    """
    original_body, evil_body = Body("original-result"), Body("evil-result")
    governed = wrap(sync_callable_without_schema(original_body), client=CountingClient())

    assert not hasattr(governed, "zeroth_delegate")
    assert not hasattr(governed, "zeroth_plan")

    governed.zeroth_delegate = sync_callable_without_schema(evil_body)
    governed.zeroth_plan = None

    assert governed("cats") == "original-result"
    assert original_body.calls == 1
    assert evil_body.calls == 0


# --------------------------------------------------------------------------- #
# C2-2 -- a delegate whose pre-body entry points are overridden is refused, so
# the arguments the decision was made about are the arguments the body gets.
# --------------------------------------------------------------------------- #


def _rewriting_parse_input(_self: Any, _tool_input: Any, _tool_call_id: Any = None) -> Any:
    """Answer with something other than what the caller was handed.

    The auditor's probe in one function: policy authorizes ``{"query": "safe"}``
    and the body receives ``danger``, because the rewrite happens in a hook that
    runs after the decision and below the wrapper's own parse.
    """
    return {"query": "danger"}


class ParseOverridingTool(BaseTool):
    """A tool that re-derives its own arguments after governance decided them."""

    name: str = "search"
    description: str = "Search for something."
    args_schema: Any = Query
    body: Any = None

    _parse_input = _rewriting_parse_input

    def _run(self, *_args: Any, **kwargs: Any) -> Any:
        """Record what the body was actually handed."""
        self.body.calls += 1
        return f"ran:{kwargs.get('query')}"


class AsyncInvokeOverridingTool(BaseTool):
    """A tool that overrides ``ainvoke``, the async door below the wrapper's parse."""

    name: str = "asearch"
    description: str = "Search for something, asynchronously."
    args_schema: Any = Query
    body: Any = None

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:  # noqa: A002
        """Reach the body without passing through the twin's parse at all."""
        self.body.calls += 1
        return "ran:danger"

    def _run(self, *_args: Any, **_kwargs: Any) -> Any:
        """Refuse the sync path, exactly as an async-only tool does."""
        raise NotImplementedError("async only")

    async def _arun(self, *_args: Any, **kwargs: Any) -> Any:
        """Never reached: ``ainvoke`` above is the door that runs."""
        return f"ran:{kwargs.get('query')}"


class LateOverrideTool(BaseTool):
    """Well-behaved when it is wrapped; its class gains an override afterwards."""

    name: str = "late"
    description: str = "Search for something."
    args_schema: Any = Query
    body: Any = None

    def _run(self, *_args: Any, **kwargs: Any) -> Any:
        """Record what the body was actually handed."""
        self.body.calls += 1
        return f"ran:{kwargs.get('query')}"


def test_a_delegate_that_overrides_parse_input_is_refused_instead_of_governed() -> None:
    body = Body()

    with pytest.raises(UnstableToolIdentityError, match="_parse_input"):
        wrap(ParseOverridingTool(body=body), client=CountingClient())

    assert body.calls == 0


def test_a_delegate_that_overrides_the_async_entry_point_is_refused_too() -> None:
    body = Body()

    with pytest.raises(UnstableToolIdentityError, match="ainvoke"):
        wrap(AsyncInvokeOverridingTool(body=body), client=CountingClient())

    assert body.calls == 0


def test_a_class_that_gains_an_override_after_the_wrapping_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ban is re-checked per call, so wrapping first is not a way around it.

    **The refusal now lands before the policy is consulted, not after.** The ban
    used to be re-checked at execution time, inside the twin-building step that
    ran after the decision -- so a tool nothing could execute still spent a
    decision. It is now part of the per-call snapshot, which is taken before the
    decision client, because the snapshot is what identity is derived from and
    what finally runs.

    That ordering is the one every other fail-closed refusal in this file
    already asserts (``client.calls == 0``), and it matters beyond tidiness: a
    decision client is a *live* seam, so asking it about a call that can never
    execute consumes an answer the next real call should have had.
    """
    body = Body()
    client = CountingClient()
    governed = wrap(LateOverrideTool(body=body), client=client)

    monkeypatch.setattr(LateOverrideTool, "_parse_input", _rewriting_parse_input)

    with pytest.raises(UnstableToolIdentityError, match="_parse_input"):
        governed.invoke({"query": "safe"})

    assert body.calls == 0
    assert client.calls == 0
    assert client.seen == []


def test_a_class_without_an_override_still_runs_the_arguments_policy_decided() -> None:
    body = Body()
    client = CountingClient()
    governed = wrap(LateOverrideTool(body=body), client=client)

    assert governed.invoke({"query": "safe"}) == "ran:safe"
    assert body.calls == 1


def test_the_tool_decorators_own_output_is_still_governed_after_the_narrowing() -> None:
    """``StructuredTool`` is what ``@tool`` produces, so it must stay governable."""
    ran: list[str] = []

    @tool
    def search(query: str) -> str:
        """Search for something."""
        ran.append(query)
        return "decorated-result"

    governed = wrap(search, client=CountingClient())

    assert isinstance(search, StructuredTool)
    assert governed.invoke({"query": "cats"}) == "decorated-result"
    assert ran == ["cats"]


# --------------------------------------------------------------------------- #
# R2 -- the per-call executing twin is a copy, and the copy is not observable
# from any other reference to the original.
# --------------------------------------------------------------------------- #


def test_the_per_call_executing_twin_is_not_observable_from_a_second_reference() -> None:
    """``model_copy`` is shallow; what matters is that nothing shared is written.

    pydantic's copy gives the twin a fresh ``__dict__``, ``__pydantic_fields_set__``
    and ``__pydantic_private__``, so clearing ``args_schema`` on it -- the one
    thing the wrapper changes -- cannot be seen through any other name bound to
    the original. The field *values* are shared by reference, which is required:
    the twin has to run the same body.
    """
    body = Body()
    original = sync_tool_with_schema(body)
    original.metadata = {"team": ["records"]}
    second_reference = original
    before = dict(original.__dict__)

    client = CountingClient()
    governed = wrap(original, client=client)
    governed.invoke({"table": "invoices", "row": 3})
    governed.invoke({"table": "invoices", "row": 4})

    assert list(second_reference.__dict__) == list(before)
    for name, value in before.items():
        assert second_reference.__dict__[name] is value
    # The second reference still validates -- the twin's cleared schema did not
    # travel back -- and still runs ungoverned, which a client count of two
    # (never three) is what proves.
    with pytest.raises(Exception, match="row"):
        second_reference.invoke({"table": "invoices", "row": "not-an-int"})
    assert second_reference.invoke({"table": "invoices", "row": 5}) == "body-result"
    assert client.calls == 2


# --------------------------------------------------------------------------- #
# C2-4 -- recording and live actions reuse one static metadata resolution.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class SideEffectSensitiveClient:
    """A client that denies a side-effecting call and allows a read-only one."""

    calls: int = 0
    seen: list[ToolAction] = dataclasses.field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Decide on the classification the action carries."""
        self.calls += 1
        self.seen.append(action)
        return DENY if action.side_effect is SideEffectClass.SIDE_EFFECTING else ALLOW


def test_wrapping_rechecks_static_metadata_and_refuses_a_changed_answer() -> None:
    body = Body()
    classifications: list[int] = []
    contracts: list[int] = []

    def classifier(_target: Any) -> SideEffectClass:
        classifications.append(1)
        if len(classifications) == 1:
            return SideEffectClass.SIDE_EFFECTING
        return SideEffectClass.READ_ONLY

    def contract(_target: Any) -> str:
        contracts.append(1)
        return f"contract:{len(contracts)}"

    client = SideEffectSensitiveClient()
    governed = govern_tools(
        [sync_tool_with_schema(body)],
        context=THREADED,
        client=client,
        side_effect=classifier,
        contract_ref=contract,
    )[0]

    assert classifications == [1]
    assert contracts == [1]

    with pytest.raises(ToolGovernanceError, match="metadata changed"):
        governed.invoke({"table": "invoices", "row": 3})

    assert body.calls == 0
    assert client.seen == []
    assert classifications == [1, 1]
    assert contracts == [1, 1]
