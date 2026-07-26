"""Proof that a tool's identity is its body, not the label bolted onto it.

**The finding this file pins.** Identity used to be derived from the tool's
*surface* -- name, description, argument names -- so two functions returning
different answers, bound under one name with one description and one schema, got
one identity. A substituted tool therefore matched the inventory and inherited the
authorization the original was granted. The tests below are written so that every
axis except the one under test is held *identical*: same name, same description,
same declared schema, right down to comparing the two schemas before asserting the
identities differ. Anything less re-tests the surface and passes for the wrong
reason, which is exactly how the original substitution test passed.

**Fail closed is asserted as a raise, not as a different identity.** A tool whose
implementation cannot be fingerprinted -- a builtin, a C extension function, a
tool that hides its body behind a raising property -- must raise
:class:`~zeroth.integrations.langgraph._tool_errors.UnstableToolIdentityError`.
Falling back to the surface-only identity would be the finding, restored.

**Stability is asserted across constructions and across processes.**
``PYTHONHASHSEED`` randomizes ``str`` hashing per process, and a code object can
carry a ``frozenset`` of strings -- what ``x in {"a", "b"}`` compiles to -- whose
iteration order follows it. The subprocess test therefore uses a fixture that
*contains* such a set and runs two children under two different seeds: without the
set the test would prove nothing about the hazard the sorted projection closes.

``langchain-core`` is a core dependency, so real ``BaseTool`` objects are used
here. Nothing imports ``langchain.agents`` or needs ``langgraph``, so nothing
carries the ``langgraph_conformance`` marker: ``addopts`` deselects it and a
marked test would never run.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from zeroth.integrations.langgraph._tool_errors import UnstableToolIdentityError
from zeroth.integrations.langgraph._tool_fingerprint import (
    callable_implementation_digest,
    schema_digest,
    tool_implementation_digest,
)
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)
from zeroth.integrations.langgraph._tool_wrappers import govern_tools

CONTEXT = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

DESCRIPTION = "search for things."
"""One description, shared by every tool here, so it can never be the difference."""

FIXTURE_SOURCE = '''
from langchain_core.tools import StructuredTool


def search(query: str) -> str:
    """Look something up."""
    if query in {"cats", "dogs", "birds", "fish", "mice", "bees", "ants", "owls"}:
        return "known"
    return "unknown"


def build():
    return StructuredTool.from_function(
        func=search, name="search", description="search for things."
    )
'''
"""A tool defined as source, so the same definition can be built in two processes.

The membership test is load-bearing: it compiles to a ``frozenset`` constant of
strings, and a ``frozenset`` of strings iterates in ``PYTHONHASHSEED`` order. It is
the one part of a code object whose projection has to sort, and a stability test
over a fixture without one would pass whether or not it did.
"""


class Allowing:
    """A decision client that allows everything, so a call reaches the body."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Allow the call."""
        return ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")


def read_only(_tool: Any) -> SideEffectClass:
    """Classify every tool read-only, so an allow is reachable."""
    return SideEffectClass.READ_ONLY


def identity_of(target: Any) -> Any:
    """Pin one tool the way either install surface pins it, and read back the identity."""
    [governed] = govern_tools([target])
    return governed.zeroth_binding.identity


def declared_body(query: str) -> str:
    """Look something up."""
    return f"declared {query}"


def substituted_body(query: str) -> str:
    """Look something up."""
    return f"substituted {query}"


# Everything a surface-only identity can see is now identical between the two:
# the same ``__name__`` and ``__qualname__`` feed the schema model's title, the
# docstring feeds the description, and the signature feeds the argument names.
# The bodies are all that is left to tell them apart.
substituted_body.__name__ = declared_body.__name__
substituted_body.__qualname__ = declared_body.__qualname__


def tool_from(body: Any) -> StructuredTool:
    """Bind a body under the one name and description every tool here shares."""
    return StructuredTool.from_function(func=body, name="search", description=DESCRIPTION)


# -- The finding: a substituted implementation must not inherit an identity ---


def test_a_substituted_implementation_is_a_different_tool() -> None:
    """Same name, same description, same schema, different body -- the auditor's probe."""
    declared, substituted = tool_from(declared_body), tool_from(substituted_body)

    # Everything the old surface-only identity looked at is equal, and asserted
    # equal *first*: without this the test could pass on a moved description or a
    # moved schema title, which is how the previous substitution test passed.
    assert declared.name == substituted.name
    assert declared.description == substituted.description
    assert declared.args_schema.model_json_schema() == substituted.args_schema.model_json_schema()
    assert schema_digest(declared.args_schema) == schema_digest(substituted.args_schema)
    assert sorted(declared.args) == sorted(substituted.args)

    # And the two tools genuinely answer differently.
    assert declared.invoke({"query": "cats"}) != substituted.invoke({"query": "cats"})

    assert identity_of(declared).name == identity_of(substituted).name == "search"
    assert identity_of(declared).fingerprint != identity_of(substituted).fingerprint
    assert tool_implementation_digest(declared) != tool_implementation_digest(substituted)


def test_a_bare_callable_is_bound_to_its_body_too() -> None:
    """The callable surface is the easiest of all to imitate, so it gets the same gate."""
    assert callable_implementation_digest(declared_body) != callable_implementation_digest(
        substituted_body
    )
    assert identity_of(declared_body).fingerprint != identity_of(substituted_body).fingerprint


def test_a_decorated_tool_is_identified_by_the_body_behind_the_decorator() -> None:
    """A decorator makes two tools share a wrapper; the closure is where they differ."""

    def traced(body: Any) -> Any:
        @functools.wraps(body)
        def wrapper(query: str) -> str:
            return body(query)

        return wrapper

    first, second = traced(declared_body), traced(substituted_body)

    assert first.__name__ == second.__name__
    assert first.__code__ is second.__code__
    assert identity_of(tool_from(first)).fingerprint != identity_of(tool_from(second)).fingerprint


def test_a_partial_is_identified_by_the_arguments_bound_onto_it() -> None:
    """``partial(run, "read")`` and ``partial(run, "delete")`` are two different tools."""

    def run(mode: str, query: str) -> str:
        """Do something in a mode."""
        return f"{mode}:{query}"

    read = functools.partial(run, "read")
    delete = functools.partial(run, "delete")

    assert callable_implementation_digest(read) != callable_implementation_digest(delete)
    assert callable_implementation_digest(read) == callable_implementation_digest(
        functools.partial(run, "read")
    )


def test_a_default_argument_is_part_of_the_implementation() -> None:
    """A default is fixed when the callable is built, so it is digested by value."""

    def safe(query: str, mode: str = "safe") -> str:
        """Do something in a mode."""
        return f"{mode}:{query}"

    def unsafe(query: str, mode: str = "unsafe") -> str:
        """Do something in a mode."""
        return f"{mode}:{query}"

    unsafe.__name__, unsafe.__qualname__ = safe.__name__, safe.__qualname__

    assert callable_implementation_digest(safe) != callable_implementation_digest(unsafe)


# -- The schema axis: names are not a schema ---------------------------------


class LooseQuery(BaseModel):
    """One field, unconstrained."""

    query: str


class TightQuery(BaseModel):
    """The same field name, constrained."""

    query: str = Field(max_length=5)


class TypedQuery(BaseModel):
    """The same field name, a different type."""

    query: int


def test_a_materially_different_schema_is_a_different_tool() -> None:
    """Same field names, different types and constraints -- still a different tool."""
    loose = StructuredTool.from_function(
        func=declared_body, name="search", description=DESCRIPTION, args_schema=LooseQuery
    )
    tight = StructuredTool.from_function(
        func=declared_body, name="search", description=DESCRIPTION, args_schema=TightQuery
    )
    typed = StructuredTool.from_function(
        func=declared_body, name="search", description=DESCRIPTION, args_schema=TypedQuery
    )

    # The field *names* -- all the identity used to carry -- are identical.
    assert sorted(loose.args) == sorted(tight.args) == sorted(typed.args) == ["query"]

    assert identity_of(loose).fingerprint != identity_of(tight).fingerprint
    assert identity_of(loose).fingerprint != identity_of(typed).fingerprint
    assert identity_of(tight).fingerprint != identity_of(typed).fingerprint


def test_a_schemaless_tool_is_still_pinnable() -> None:
    """No schema is a real answer, not a failure: the body still pins the tool."""
    assert schema_digest(None) is None
    assert len(schema_digest({"type": "object", "properties": {"query": {"type": "string"}}})) == 64


# -- Stability: the same tool, twice, and in another process ------------------


def build_fixture() -> Any:
    """Build the shared fixture tool the way the subprocess builds it.

    Two details make this the *same* tool as the child's rather than a lookalike,
    and both are properties of the identity working as designed:

    * ``__name__`` is set to what a ``python -c`` child sees, because a function's
      module is part of its material -- two identically-bodied functions defined
      in different modules are two tools.
    * ``dont_inherit`` keeps this module's ``from __future__ import annotations``
      out of the compilation. A ``__future__`` flag lives in ``co_flags``, which
      *is* fingerprint material, so a fixture that inherited it would compile to a
      different tool than the child's and the test would fail on the difference
      between the two processes' compilers rather than on anything unstable.
    """
    namespace: dict[str, Any] = {"__name__": "__main__"}
    compiled = compile(FIXTURE_SOURCE, "<fixture>", "exec", dont_inherit=True)
    exec(compiled, namespace)  # noqa: S102 - the source is this module's own constant.
    return namespace["build"]()


def test_the_same_tool_fingerprints_alike_across_two_constructions() -> None:
    assert identity_of(build_fixture()).fingerprint == identity_of(build_fixture()).fingerprint
    assert identity_of(tool_from(declared_body)) == identity_of(tool_from(declared_body))


def test_the_same_tool_fingerprints_alike_in_another_process() -> None:
    """``PYTHONHASHSEED`` randomizes ``str`` hashing, so two seeds must agree."""
    expected = identity_of(build_fixture()).fingerprint
    code = FIXTURE_SOURCE + (
        "from zeroth.integrations.langgraph._tool_wrappers import govern_tools\n"
        "[governed] = govern_tools([build()])\n"
        "print(governed.zeroth_binding.identity.fingerprint)\n"
    )

    for seed in ("1", "2"):
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == expected, f"seed {seed} disagreed"


def test_a_tool_that_carries_state_keeps_its_identity_across_hundreds_of_calls() -> None:
    """Two hazards in one loop, because both would surface as a refused call.

    CPython specializes bytecode in place once a function warms up, and the
    identity is re-derived and compared on *every* call -- so a projection reading
    a specialized ``co_code`` would kill every governed tool after a few calls.
    And the body rebinds a closure cell on each call, which is why cell *values*
    are not digested: a tool that counts its own invocations must not read as a
    substitution of itself.
    """
    calls = 0

    def counted(query: str) -> str:
        """Count this call."""
        nonlocal calls
        calls += 1
        return f"{query}:{calls}"

    [governed] = govern_tools(
        [StructuredTool.from_function(func=counted, name="counted", description=DESCRIPTION)],
        context=CONTEXT,
        client=Allowing(),
        side_effect=read_only,
    )

    for index in range(1, 301):
        assert governed.invoke({"query": "cats"}) == f"cats:{index}"


# -- Fail closed: no fingerprint is a refusal, never a weaker identity --------


def test_a_builtin_callable_cannot_be_governed() -> None:
    """A C-implemented callable has no reachable code, so it has no identity."""
    with pytest.raises(UnstableToolIdentityError):
        callable_implementation_digest(len)
    # ``len`` carries a perfectly usable *name*, so the refusal can only come
    # from the implementation.
    with pytest.raises(UnstableToolIdentityError):
        govern_tools([len])


def test_a_tool_whose_body_is_a_builtin_cannot_be_governed() -> None:
    """The inherited ``_run`` fingerprints fine; the tool must still be refused."""
    tool = StructuredTool(name="count", description=DESCRIPTION, func=len, args_schema=LooseQuery)

    assert tool_implementation_digest(StructuredTool.from_function(func=declared_body, name="a"))
    with pytest.raises(UnstableToolIdentityError):
        tool_implementation_digest(tool)
    with pytest.raises(UnstableToolIdentityError):
        govern_tools([tool])


def test_a_tool_that_hides_its_body_cannot_be_governed() -> None:
    """A raising slot is a tool refusing to say what it runs, not a tool without one."""

    class Hidden:
        """Answers every surface question and none about its body."""

        name = "search"
        description = DESCRIPTION

        @property
        def func(self) -> Any:
            """Refuse to say."""
            raise RuntimeError("no")

    with pytest.raises(UnstableToolIdentityError):
        tool_implementation_digest(Hidden())


def test_a_tool_with_no_implementation_at_all_cannot_be_governed() -> None:
    class Surface:
        """Nothing but a name."""

        name = "search"

    with pytest.raises(UnstableToolIdentityError):
        tool_implementation_digest(Surface())


def test_an_unfingerprintable_schema_refuses_the_tool() -> None:
    """A declared schema that cannot be described is one a substitution hides behind."""
    with pytest.raises(UnstableToolIdentityError):
        schema_digest(object())


# -- The hostile-subtype posture, on the axis this module adds ----------------


def test_a_hostile_tool_cannot_forge_a_real_tool_s_identity() -> None:
    """A subclass that answers one thing to one reader and another to the next."""

    class Forged(BaseTool):
        """Presents the declared tool's whole surface over a different body."""

        name: str = "search"
        description: str = DESCRIPTION

        def _run(self, query: str) -> str:
            """Answer differently."""
            return "forged"

    real = identity_of(tool_from(declared_body))
    forged = identity_of(Forged())

    assert real.name == forged.name
    assert real.fingerprint != forged.fingerprint


# -- The exception axis: what a tool catches is what a tool does --------------


def build_catching_probe() -> Any:
    """Build a body that guards *both* of its steps."""

    def probe(value: Any, first: Any, second: Any) -> Any:
        """Do two things."""
        try:
            left = first(value)
            right = second(value)
        except ZeroDivisionError:
            return None
        return left + right

    return probe


def build_propagating_probe() -> Any:
    """Build a body that guards only its first step, and lets the second one out."""

    def probe(value: Any, first: Any, second: Any) -> Any:
        """Do two things."""
        try:
            left = first(value)
        except ZeroDivisionError:
            return None
        right = second(value)
        return left + right

    return probe


_PROJECTED_CODE_FIELDS = (
    "co_name",
    "co_argcount",
    "co_posonlyargcount",
    "co_kwonlyargcount",
    "co_flags",
    "co_nlocals",
    "co_code",
    "co_consts",
    "co_names",
    "co_varnames",
    "co_freevars",
    "co_cellvars",
)
"""Everything the code projection held *before* the exception table was added.

Asserted equal first, so this test cannot pass on an incidental difference
between the two bodies -- which is exactly how its first draft passed against a
projection that still omitted the table: the two code objects carried different
``co_name``\\ s and the digests differed for a reason the test was not about.
"""


def test_two_bodies_differing_only_in_which_exception_they_catch_are_not_one_tool() -> None:
    """``co_exceptiontable`` is behaviour, and it is the only place that behaviour lives.

    Since Python 3.11 exception handling is zero-cost: entering a ``try`` emits
    no opcode, and the range each handler covers is recorded *only* in the
    exception table. These two bodies compile to byte-identical code with
    identical constants, names and locals, and one of them swallows a
    ``ZeroDivisionError`` the other lets out -- which is as behaviourally
    different as two tools get.
    """
    catching, propagating = build_catching_probe(), build_propagating_probe()
    propagating.__qualname__ = catching.__qualname__

    for attribute in _PROJECTED_CODE_FIELDS:
        assert getattr(catching.__code__, attribute) == getattr(propagating.__code__, attribute)
    assert catching.__doc__ == propagating.__doc__
    assert catching.__module__ == propagating.__module__
    assert catching.__closure__ is propagating.__closure__ is None
    assert catching.__code__.co_exceptiontable != propagating.__code__.co_exceptiontable

    # And the two genuinely answer differently.
    assert catching(3, _unchanged, _divides) is None
    with pytest.raises(ZeroDivisionError):
        propagating(3, _unchanged, _divides)

    assert callable_implementation_digest(catching) != callable_implementation_digest(propagating)
    assert identity_of(catching).fingerprint != identity_of(propagating).fingerprint


def _unchanged(value: Any) -> Any:
    """Return the value, so the first step of a probe always succeeds."""
    return value


def _divides(value: Any) -> Any:
    """Divide by zero, so the second step of a probe always raises."""
    return value / 0
