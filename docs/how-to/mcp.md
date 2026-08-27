# Using MCP tools

## Overview

An MCP (Model Context Protocol) server is an external process that advertises a
suite of tools over stdio. Zeroth attaches those tools to an agent as **graph
nodes** — one `mcp_tool` node per tool — so a tool an external process defines
still has a contract at publish time rather than only on the day of the run.

That shape follows from a split of authority. An **operator** registers the
server: its command, its arguments, its environment, and the capabilities it may
be asked for. An **author** references it by `ref` and writes none of those.
Three consequences run through the rest of this page:

1. What an author may declare is bounded by what the operator granted.
2. What a tool looks like is frozen at import and re-checked before every call.
3. What a call *guarantees* is weaker than an executable unit's — deliberately,
   and visibly in the audit trail.

The workflow is **register → import → publish → run**.

## Install

Nothing extra. The `mcp` client is a core dependency of `zeroth-core`, not an
optional extra. What you do need is a service bootstrapped the normal way
(`bootstrap_service` wires the registry, the publish-time grants resolver and the
run-scoped session pool together) and an API key holding `mcp:admin` for the two
registry steps.

## 1. Register the server

An operator posts the registration. `ref` must match `^[a-z0-9_-]{1,64}$`, and a
ref that already exists answers `409` — use `PUT` to reconfigure it.

```bash
curl -s -X POST http://localhost:8000/v1/mcp/servers \
  -H "X-API-Key: $MCP_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "ref": "docs-search",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/docs"],
        "env": {},
        "grants": ["process_spawn", "external_api_call"]
      }'
```

Registering does not spawn anything. Listing the server's tools is the one route
that runs the operator's command, and it is why the whole route set is
admin-tier:

```bash
curl -s http://localhost:8000/v1/mcp/servers/docs-search/tools \
  -H "X-API-Key: $MCP_ADMIN_KEY"
```

Each entry comes back with the tool's name, description, input schema, and the
`schema_hash` an import would pin. Every `env` **value** is masked in every
response — an MCP server's environment is credentials by convention, so there is
no non-secret half worth showing. `PUT` with no `env` key keeps the stored
environment; `"env": {}` explicitly clears it.

## 2. Import the tools into a draft graph

```bash
uv run zeroth-core mcp-import \
  --server docs-search \
  --graph support-triage \
  --agent researcher \
  --tool read_file \
  --tool list_directory
```

Omit `--tool` to import every tool the server offers; `--tenant` selects the
owning tenant. For each tool the importer spawns the server once, takes its tool
list, stops it, and writes into the draft:

- one `MCPToolNode` carrying `server_ref`, `tool_name`, the description, the
  `input_schema` and the `schema_hash` computed over all three;
- a `tool`-kind `Edge` from the agent to that node, plus an `AgentToolBinding`
  so the model can call it by name;
- `process_spawn` and `external_api_call` in the node's `capability_bindings`,
  because publish and the runtime both demand them (the floor, below);
- the same pair added to the *agent's* `capability_bindings` if it lacks them,
  because the agent floor demands that too. Adding rather than refusing is the
  point of the command: what it writes should publish and run without anyone
  hand-editing JSON, and an agent's own bindings have no ceiling to widen past.

The import either writes a graph that would publish or writes nothing — a draft
that cannot publish is worse than no change, because the author finds out later
and further from the cause. It refuses an unregistered server, a **server whose
grants do not cover the floor** (`grants: []` is the default for a newly
registered server, so this is the first wall a new operator hits), a graph that
is not a draft, a missing agent node, and a named tool the server does not
offer. Every way of failing to reach the server — a command not on `PATH`, a
command that is not an MCP server, one that connects and never answers — is
reported as an import failure rather than a raw traceback.

Each advertised declaration also goes through the same model-boundary screen the
runtime applies, because a tool's description and the prose in its schema are
text an external process chose and they land in the model's instruction surface
on every step. A declaration whose bounds cannot be represented fails the import.
A heuristic flag does not: the heuristics are conservative, and refusing on one
would silently remove a legitimate capability, so flags are reported and the
import proceeds. What gets **pinned is the raw declaration**, not the screened
one — the runtime hashes the live server's raw text, so a pin taken over a
display transform would read as permanent drift.

Re-importing the same server is idempotent — an existing node for a
`(server_ref, tool_name)` pair is updated in place, keeping its edge and its
binding, rather than gaining a second copy under a suffixed id. That is what
makes "re-import to accept a schema change" a safe instruction.

## 3. Publish — the capability model

Publish is where the operator's ceiling is enforced. Take an `mcp_tool` node
**M** referencing server **S**, whose operator-declared grants are **G**, bound
as a tool by agent **A**. `caps(M)` is what M's own `capability_bindings`
resolve to; `effective(A)` is what `PolicyGuard` yields for A.

| Rule | Statement | Why it exists |
|---|---|---|
| **Floor** | `caps(M) ⊇ {process_spawn, external_api_call}` | Reaching a server spawns a subprocess that then calls out to an external service. That is what the mechanism does, not a policy choice. Without the floor, declaring *nothing* was the cheapest way past publish — it exceeded nothing — and the run then failed at dispatch instead. |
| **Ceiling** | `caps(M) ⊆ G` | `capability_bindings` are author-declared, so an author who wants a capability simply writes it. `G` is the one side of this comparison the author cannot edit. |
| **Agent floor** | `effective(A) ⊇ caps(M)` | The agent that binds the tool must itself hold everything the tool node declares, or an agent granted nothing could reach a server through a node that declares the pair. |

Two things about this are easy to get wrong.

**The ceiling's subject is the node, never the agent.** Comparing
`effective(A)` against `G` sounds equivalent and is not. An agent holds
capabilities for everything *else* it does, so that comparison would force an
operator to widen `G` until it covered the union of every capability any
referencing agent happens to hold — converging the ceiling on "everything" and
dissolving the control by following its own advice.

**`grants: []` denies every referencing node.** An unfilled ceiling reads as
*asserted nothing*, not as *no ceiling*.

One more subject distinction is worth carrying: at publish the agent floor
compares the agent's *author-declared* `capability_bindings`, while the runner's
tool gate compares the effective set `PolicyGuard` yields — which a policy bound
in the same graph can make smaller. So passing publish is not a promise of never
being denied at dispatch. What it does buy is that no tool's requirement is
*invisible* at author time: publish and the runner read the required set from
the same place.

The ceiling is checked again at run time, before a server process exists, and
**unconditionally** — including on a deployment running capabilities in advisory
mode. `caps(M)` is static graph data and `G` is operator-owned, so neither
depends on a policy switch they have nothing to do with. It also cannot be left
to publish alone: a published graph version is immutable, so a node validated
against yesterday's grants would otherwise keep a capability the operator has
since withdrawn. Narrowing `grants` under an already-published graph strands it
on purpose — it fails closed on the next run.

The session pool re-checks the agent floor too — that the *calling agent* holds
`process_spawn` and `external_api_call` — but that one is skipped when
enforcement is unwired, matching the runner's own convention. It restates what
the runner's tool gate already demands of the agent, and is kept as the gate
still standing if the tool gate is ever bypassed.

## 4. Run

Nothing extra to wire. The run owns its MCP sessions:

- one process per distinct `server_ref` per run, spawned on the first tool call
  that needs it, so two agents referencing the same server share one process;
- stopped once when the run ends, whether it completed, failed, or paused at an
  approval gate;
- a resumed run gets a fresh pool — a subprocess cannot survive the pause that
  produced the checkpoint, and pretending otherwise would leak a dead handle;
- before the call, the tool's live shape is compared against the pinned
  `schema_hash`, and a mismatch refuses the call rather than proceeding.

### What an `mcp_tool` call does not guarantee

It is **at-least-once**. The call carries no operation identity, no receipt, no
replay suppression, and no reconciliation path. A retried agent turn calls the
tool a second time and nothing suppresses the duplicate; a call that *failed*
may still have landed, and nobody can ask. That gap is why `mcp_tool` is its own
node kind rather than a mode on `ExecutableUnitNode`, where the weaker guarantee
would sit invisibly beside nodes that really do carry a receipt.

The gap is marked rather than implied: the tool-call audit record carries
`operation_support: at_least_once` and `operation_residual_duplicate_risk: true`.
The runtime also keeps the two kinds of failure apart, because only one of them
means "nothing happened": a call refused *before* dispatch — unknown server,
capability denial, ceiling, a spawn that never handshook, schema drift — never
reached the tool, while a call that reached the server and failed there may
already have taken effect. Closing the gap takes idempotency or an outcome query
on the server's side — see
[Delivery guarantees](../concepts/delivery-guarantees.md).

## What `grants` does not do

`grants` is a capability **assertion**, not a sandbox. It decides which graphs
may *reference* a server. It does not confine the process that server runs in:

- `command`, `args` and `env` are unbounded — any binary on the host, any
  arguments, any environment;
- the server runs as the service user, with that user's filesystem, network and
  credentials;
- once it is running, nothing in the registry limits what it does.

So `mcp:admin` is effectively **code execution as the service user**. Hand it
out the way you would hand out shell access on that host, not the way you would
hand out a workflow permission.

That is also the security argument for where the permission sits. `MCP_ADMIN` is
admin-tier and `OPERATOR` deliberately does not hold it, even though `OPERATOR`
does hold `WORKFLOW_ADMIN` (it authors graphs) and `CONNECTOR_ADMIN` (it authors
the infrastructure bindings graphs depend on). Reusing `CONNECTOR_ADMIN` for the
MCP registry would have handed the registry to the very role that writes the
graphs the registry constrains — the ceiling would have become author-editable,
which is precisely what it exists not to be.

That separation is a default, not an invariant. Custom roles configured through
`ZEROTH_SERVICE_ROLES_JSON` can name any permission, `mcp:admin` included, so a
deployment can undo it in one line of config. If you define custom roles, check
that none of them pairs `mcp:admin` with `workflow:admin`.

## The deprecated inline path

`AgentNodeData.mcp_servers` — servers written directly onto an agent node —
predates `mcp_tool` nodes and still works. Publishing a graph that uses it emits
a **warning**, not an error. The one thing the inline declaration itself can
raise as an error is an agent missing `process_spawn` or `external_api_call`.

Be exact about what that means while the path exists. On it, the author picks the
binary, the argv and the environment; there is no registry row, so **there is no
ceiling at all**; and the tools are discovered at run time, so nothing about them
is pinned, diffable or knowable at publish. The claim that *grants is the one
side of the check an author cannot edit* is true of `mcp_tool` nodes and false
of the inline path — an author who wants an unbounded server writes one inline
and publishes over a warning. Do not describe the registry as a ceiling on a
deployment that still admits inline servers without naming this exception.

To migrate: register the server with an operator, run `zeroth-core mcp-import`
against the draft, then delete the inline entry.

## Pitfalls

1. **Reading `grants` as a sandbox.** It bounds *who may reference* the server,
   not what the server may do. Confinement, if you need it, has to come from the
   host — the user the service runs as, the container it runs in.
2. **Widening `grants` to clear a capability error.** Check which subject the
   error names first. Widening because an *agent* holds an unrelated capability
   is the failure mode the ceiling was reshaped to avoid.
3. **Publishing through a validator with no grants resolver.** `GraphValidator`
   skips the entire floor-and-ceiling pass when `mcp_grants_resolver` is `None`
   — contract-only callers have no deployment to resolve a ref against — so a
   hand-built validator accepts any capability an author declares. The agent
   floor still runs, because it needs no registry. `bootstrap_service` wires the
   resolver; anything else has to wire it deliberately, and passing `None`
   explicitly disarms it just as thoroughly as omitting it.
4. **Assuming a description change is cosmetic.** The description is inside the
   pinned digest — it is model-visible text an external process controls, so a
   server that silently rewrites it has changed what the agent was pinned to.
   Expect drift refusals after a server upgrade, and re-import to accept them.
5. **Treating a failed MCP call as "nothing happened".** At-least-once cuts both
   ways: the effect may have landed. Only a rejection *before* dispatch means no
   effect occurred, and that is the full list given above — unknown server,
   capability denial, ceiling, a spawn that never handshook, schema drift.
   Anything that fails once the call has reached the server is unresolvable
   from this side.
6. **Leaving an inline `mcp_servers` entry in place after migrating.** It keeps
   publishing on a warning and keeps the un-ceilinged path open.

## Reference cross-link

Related guides: [concepts/delivery-guarantees](../concepts/delivery-guarantees.md)
· [concepts/policy](../concepts/policy.md) · [policy how-to](policy.md) ·
[agents how-to](agents.md) · [identity how-to](identity.md).

The registry routes are in the
[HTTP API reference](../reference/http-api.md) under `/v1/mcp/servers`.
