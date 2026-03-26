Zeroth is a governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services.

Structurally, Zeroth follows a graph-based builder model similar to node-driven workflow tools, but it is designed for a different class of problems: secure, typed, auditable, and operationally manageable agentic software. Users compose systems from a small set of core primitives — agent nodes, executable units, human approval nodes, and conditions — while retaining the flexibility to attach memory connectors, wrap existing code with minimal adaptation, and enforce execution policies at runtime.

The implementation of Zeroth is written using GovernAI as the foundational framework layer for governed agent orchestration and runtime control.

At its core, Zeroth treats an agentic application as an explicit executable graph rather than an opaque prompt chain. Every node boundary is typed, every executable unit runs inside a governed sandbox, memory is attachable and shareable through connector instances, and audits are recorded per node rather than buried in a monolithic trace. Agent state persistence is based on thread_id, following the same general model used in LangGraph for preserving state across multiple runs: stateful agents can resume and continue their execution context over time through a stable thread-scoped identity rather than treating every invocation as fully stateless.

The resulting system can be published and exposed through a service wrapper as an asynchronous API service suitable for real production usage.

This document defines the fine-grained technical tasks required to implement Zeroth as a modular monolith MVP, with strong emphasis on governance, sandboxed execution, typed contracts, per-node observability, thread-based state persistence, and deployment-ready architecture.

⸻

Product

Product Objective

Build a medium-code platform for visually authoring, governing, executing, and deploying agentic systems as standalone API services.

The platform should feel structurally similar to n8n in graph authoring and connector-style composition, but its objective is different: it is designed specifically for secure, governed, manageable, and production-oriented multi-agent systems.

The system must support:
	•	graph-based authoring
	•	cyclic execution
	•	three first-class node types only
	•	typed contracts using a Pydantic-like system
	•	executable units as the universal operational primitive
	•	optional attachable memory connectors for agents
	•	per-node audit logs
	•	async execution
	•	deployment through a service wrapper
	•	sandboxed execution of executable units with cached environment reuse
	•	thread-based state persistence for stateful agents

The MVP must be implemented as a modular monolith.

⸻

1. Locked Product Decisions

These are binding architecture and product constraints for MVP.

1.1 Runtime topology
	•	Implement as a modular monolith
	•	Internal subsystems must be clearly separated by domain
	•	Do not split agents, sandboxes, audits, deployment, or orchestration into separate services in MVP

1.2 Graph semantics
	•	Graphs may be cyclic
	•	Runtime must support repeated node execution
	•	Runtime must include cycle and loop safeguards
	•	Branching conditions remain a dedicated concern and are not removed into executable units

1.3 Invocation mode
	•	Runtime execution is asynchronous
	•	The deployed API must expose async invocation and status retrieval
	•	Do not design the system around sync-only request blocking

1.4 Node primitives

Only three first-class runtime node types must exist:
	•	agent
	•	executable_unit
	•	human_approval

Condition handling remains separate, but all other operational logic such as:
	•	transformations
	•	adapters
	•	routing helpers
	•	data shaping
	•	deterministic processing
	•	integration calls
	•	reducers
	•	formatting

must be implemented through executable_unit.

1.5 Contracts
	•	Node input/output contracts must use a Pydantic-like validation model
	•	Runtime must validate payloads at node boundaries
	•	Edge mappings are explicit and persisted

1.6 Memory
	•	Agents may optionally attach memory connectors
	•	Memory is not assumed globally
	•	Multiple agents may attach to the same memory connector instance
	•	If multiple agents attach to the same connector instance, they share memory
	•	Memory attachment model should resemble n8n-style connector semantics

1.7 Agent state persistence
	•	Stateful agent continuity is keyed by thread_id
	•	thread_id must persist agent-relevant state across multiple runs
	•	A run may start a new thread or continue an existing one
	•	Thread-based persistence is distinct from transient per-run execution state

1.8 Audits
	•	Audits are per-node
	•	Audit storage and ownership is node-local execution history
	•	Aggregation by run or graph is allowed, but audit design must not collapse into one centralized opaque run log

1.9 Judge / evaluation subsystem
	•	Exclude the judge subsystem from MVP
	•	Preserve extension points for future evaluation, but do not build MVP around it

1.10 Deployment
	•	Deployed graphs are exposed via a service wrapper
	•	The wrapper invokes the platform runtime against a published graph version
	•	Deployment artifact is not a fully independent reimplementation of the graph

1.11 Executable unit execution
	•	Every executable unit must run in a sandbox
	•	Sandbox environments should be cached and reused
	•	Environment reuse should be keyed by runtime and dependency/build identity
	•	User code adaptation should be minimal
	•	Existing scripts, projects, binaries, and commands must be onboardable via a governable manifest, not heavy rewrites

1.12 Executable unit onboarding modes for MVP

Support three user-facing executable unit modes:
	•	Native Unit — code written directly in platform
	•	Wrapped Command Unit — existing script, binary, or command with manifest
	•	Project Unit — uploaded project/archive with build + run manifest

⸻

2. Product Thesis

The platform is a graph-native operating environment for production agent systems.

It is not:
	•	a generic no-code automation tool
	•	a chat UI builder
	•	a prompt playground
	•	an ungoverned autonomous agent sandbox

It is:
	•	a medium-code platform for building governed agentic backends
	•	a graph-based runtime for typed multi-agent systems
	•	a controlled execution platform for code-backed and agent-backed workflows
	•	a deployment environment for shipping those workflows as API services

The system must optimize for:
	•	explicitness over hidden magic
	•	governance over permissive flexibility
	•	manageability over novelty
	•	compatibility with existing code
	•	auditability over opaque orchestration
	•	explicit state persistence over hidden in-memory behavior

⸻

3. Core Domain Model

3.1 Graph

A graph is the canonical application definition.

Graph must include
	•	graph ID
	•	graph name
	•	graph version
	•	graph status: draft / published / archived
	•	node collection
	•	edge collection
	•	graph metadata
	•	execution settings
	•	policy bindings
	•	deployment settings
	•	service exposure settings

Graph execution settings must include
	•	max total steps
	•	max total runtime
	•	max visits per node
	•	optional max visits per edge
	•	default timeout policies
	•	failure policy defaults
	•	audit defaults

⸻

3.2 Node

A node is one of:
	•	agent
	•	executable_unit
	•	human_approval

Every node must include
	•	node ID
	•	graph version reference
	•	node type
	•	node version
	•	display metadata
	•	input model reference
	•	output model reference
	•	execution config
	•	policy bindings
	•	capability bindings
	•	audit config

Agent-specific node data
	•	instruction block
	•	model/provider config
	•	tool attachments
	•	memory connector attachments
	•	retry policy
	•	timeout policy
	•	state persistence config
	•	thread participation mode

Executable-unit-specific node data
	•	executable unit manifest reference
	•	execution mode
	•	runtime bindings
	•	sandbox config
	•	output extraction strategy

Human-approval-specific node data
	•	approval payload schema
	•	resolution schema
	•	approval policy config
	•	pause behavior config

⸻

3.3 Condition

Conditions remain a dedicated concern.

Condition subsystem must support
	•	conditional branching based on structured outputs
	•	edge or branch activation rules
	•	explicit evaluation semantics
	•	compatibility with cyclic execution

Important

Conditions should not explode into many extra node primitives.
They may exist as a specialized graph/runtime construct, but the visible primary node taxonomy remains limited.

⸻

3.4 Edge

An edge connects nodes and defines payload transfer semantics.

Each edge must include
	•	edge ID
	•	source node ID
	•	target node ID
	•	mapping config
	•	condition binding if applicable
	•	enabled/disabled flag
	•	metadata

Edge behavior
	•	output from source node is transformed through edge mapping
	•	condition is evaluated if attached
	•	target input is validated after mapping

⸻

3.5 Memory Connector

Memory connectors are attachable resources for agent nodes.

Memory connector must include
	•	connector ID
	•	connector type
	•	connector config
	•	scope
	•	read/write permissions
	•	retention policy
	•	serialization contract if applicable

Supported MVP memory connector classes
	•	ephemeral run memory
	•	key-value memory
	•	conversation/thread memory
	•	shared memory instance semantics

⸻

3.6 Executable Unit

Executable units are the universal operational primitive for non-approval, non-agent execution logic.

Executable unit must include
	•	unit ID
	•	unit version
	•	onboarding mode
	•	runtime/language
	•	manifest
	•	input contract
	•	output contract
	•	capabilities
	•	sandbox requirements
	•	build config if relevant
	•	run config
	•	output extraction config
	•	cache identity inputs

⸻

3.7 Thread

A thread is the persistence boundary for stateful agent continuity across multiple runs.

Thread must include
	•	thread ID
	•	graph or deployment association if applicable
	•	thread status
	•	participating agent references where applicable
	•	persisted agent state snapshot or checkpoint references
	•	attached thread-scoped memory references if applicable
	•	created/updated timestamps

Thread semantics
	•	a thread may span multiple runs
	•	a run may create a new thread or continue an existing one
	•	thread state is restored before relevant agent execution and checkpointed after relevant agent execution
	•	thread state is distinct from node-local audits and run-local orchestration state

⸻

3.8 Run

A run is one execution instance of a graph version.

Run must include
	•	run ID
	•	graph version reference
	•	deployment reference if applicable
	•	optional thread ID
	•	current status
	•	current node or pending nodes
	•	execution history
	•	node visit counts
	•	condition evaluation results
	•	audit references
	•	final output or failure state
	•	created/updated timestamps

⸻

3.9 Node Audit Record

Audits are recorded per node execution.

Each node audit record must include
	•	audit ID
	•	run ID
	•	optional thread ID
	•	node ID
	•	node version
	•	attempt number
	•	mapped input snapshot or redacted representation
	•	validation results
	•	execution metadata
	•	output snapshot or redacted representation
	•	error details if any
	•	memory interactions if applicable
	•	tool calls if applicable
	•	stdout/stderr if applicable
	•	timestamps

⸻

4. Major Subsystems

The MVP must be implemented through the following modules:
	1.	Graph Authoring Module
	2.	Contract and Validation Module
	3.	Condition and Branching Module
	4.	Agent Module
	5.	Memory Connector Module
	6.	Executable Unit Module
	7.	Sandbox Execution Module
	8.	Runtime Orchestration Module
	9.	Thread and State Persistence Module
	10.	Human Approval Module
	11.	Audit Module
	12.	Policy and Capability Module
	13.	Deployment Module
	14.	Async Invocation and Run API Module

⸻

5. Graph Authoring Module

5.1 Canonical Graph Schema

Task

Implement the canonical graph schema using Pydantic-style models.

Requirements
	•	support only 3 primary node types
	•	persist conditions separately from node taxonomy
	•	support cyclic graphs
	•	support draft and published versions
	•	support graph import/export
	•	support schema versioning of graph format

Deliverables
	•	graph models
	•	graph serialization layer
	•	graph storage schema
	•	graph migration version field

⸻

5.2 Graph CRUD and Versioning

Task

Implement graph creation, editing, cloning, publishing, and archival.

Requirements
	•	draft graphs editable
	•	published graphs immutable
	•	clone published graph into new draft
	•	version history queryable
	•	graph diff support between versions

Diff must include
	•	node changes
	•	edge changes
	•	condition changes
	•	contract changes
	•	policy changes
	•	memory connector changes
	•	executable unit binding changes

Deliverables
	•	graph repository
	•	version manager
	•	graph diff engine

⸻

5.3 Graph Validation Engine

Task

Validate graph executability and integrity.

Validation rules
	•	unique node IDs
	•	all edges reference existing nodes
	•	all conditions reference valid operands or sources
	•	all input and output contracts exist
	•	all edge mappings valid
	•	deployable entrypoint defined
	•	deployable output defined
	•	tool attachments valid
	•	memory attachments valid
	•	loop safeguards defined
	•	approval nodes valid
	•	policy references valid

Important
	•	cyclic graphs are valid
	•	validator must not reject cycles by default
	•	validator must enforce presence of safety constraints for cyclic execution

Deliverables
	•	validation engine
	•	structured validation report
	•	warning/error taxonomy

⸻

6. Contract and Validation Module

6.1 Pydantic-Like Contract Registry

Task

Implement a contract registry for node input/output models.

Requirements
	•	every node references input and output models
	•	support nested typed models
	•	support required/optional fields
	•	support enums
	•	support arrays
	•	support descriptive field metadata
	•	support model versioning

Deliverables
	•	contract registry
	•	model reference system
	•	model version resolver

⸻

6.2 Runtime Validation

Task

Validate payloads at execution boundaries.

Requirements
	•	validate target input after edge mapping
	•	validate node output after execution
	•	validation errors must produce node-local audit entries
	•	validation errors must update run state

Deliverables
	•	runtime validator
	•	validation error schema
	•	audit-integrated validation wrapper

⸻

6.3 Edge Mapping System

Task

Implement explicit mapping between source output and target input.

Requirements
	•	field passthrough
	•	field rename
	•	constant injection
	•	default injection
	•	field extraction
	•	nested object assembly
	•	mapping persistence on edge
	•	mapping validation before runtime

Deliverables
	•	mapping schema
	•	mapping executor
	•	mapping validator

⸻

7. Condition and Branching Module

7.1 Condition Model

Task

Implement a dedicated condition subsystem.

Requirements
	•	conditions can evaluate structured outputs
	•	conditions can activate or deactivate branches
	•	conditions can coexist with cyclic graphs
	•	conditions should be explicit and inspectable
	•	condition evaluations must be captured in run state

Deliverables
	•	condition schema
	•	condition evaluator
	•	condition binding system

⸻

7.2 Branch Resolution

Task

Determine next nodes based on conditions and graph structure.

Requirements
	•	support one-to-one transitions
	•	support one-to-many branching
	•	support conditional fan-out
	•	support branch suppression
	•	support repeated traversal in cycles

Deliverables
	•	branch resolver
	•	next-step planner
	•	condition result recorder

⸻

8. Agent Module

8.1 Agent Runtime

Task

Implement execution runtime for agent nodes.

Requirements

Each agent must support:
	•	structured input
	•	instruction assembly
	•	model/provider invocation
	•	optional tool attachment
	•	optional memory reads/writes
	•	structured output generation
	•	output validation
	•	retry and timeout policy handling
	•	optional thread-based state restoration
	•	optional thread-based state checkpointing

Deliverables
	•	agent config model
	•	agent runner
	•	model invocation adapter
	•	output validation wrapper

⸻

8.2 Tool Attachment

Task

Allow agents to call attached executable units only.

Requirements

Each attachment must define:
	•	alias
	•	executable unit reference
	•	allowed invocation mode
	•	timeout override if any
	•	permission scope
	•	side-effect allowance

Rules
	•	no undeclared tools callable
	•	tool invocation must be auditable as part of agent node history

Deliverables
	•	tool attachment manifest
	•	agent-to-tool bridge
	•	tool permission wrapper

⸻

8.3 Agent Prompt/Instruction Assembly

Task

Build structured prompt assembly from typed input and agent config.

Requirements
	•	combine static instructions and structured runtime data
	•	support future extensibility for prompt templating
	•	capture final prompt/invocation metadata in audit-friendly form subject to redaction rules

Deliverables
	•	prompt assembly layer
	•	prompt config model
	•	redaction-aware audit serializer

⸻

9. Memory Connector Module

9.1 Memory Connector Interface

Task

Implement memory as an attachable connector abstraction.

Requirements

Each connector must declare:
	•	connector type
	•	config
	•	read interface
	•	write interface
	•	permissions
	•	scope
	•	retention behavior

Deliverables
	•	memory connector interface
	•	connector manifest model
	•	connector registry

⸻

9.2 Shared Memory Semantics

Task

Support shared memory instance behavior.

Requirements
	•	if multiple agents attach to the same connector instance, they share memory state
	•	run state must preserve connector instance references
	•	audit entries must record memory interactions per node execution

Deliverables
	•	shared memory binding model
	•	connector instance resolver
	•	memory access recorder

⸻

9.3 MVP Connector Implementations

Task

Ship initial connector implementations.

Required connectors
	•	ephemeral run-scoped memory
	•	key-value memory
	•	conversation/thread memory

Deliverables
	•	connector implementations
	•	config schemas
	•	integration tests

⸻

10. Executable Unit Module

This is a core subsystem and must be specified tightly.

10.1 Executable Unit Manifest

Task

Implement a strict executable unit manifest model.

Manifest must include
	•	unit ID
	•	version
	•	onboarding mode
	•	runtime/language
	•	artifact source
	•	build config if applicable
	•	run config
	•	entrypoint type
	•	input injection mode
	•	output extraction mode
	•	input contract
	•	output contract
	•	environment variables config
	•	dependency metadata
	•	capability requests
	•	resource limits
	•	timeout
	•	cache identity fields
	•	audit settings

Deliverables
	•	executable unit manifest schema
	•	manifest validator
	•	manifest documentation/spec

⸻

10.2 MVP Onboarding Modes

Task

Implement three executable unit onboarding modes.

Mode A — Native Unit

User writes unit code directly in platform.

Requirements
	•	Python first-class in MVP
	•	explicit entry function
	•	dependency declaration
	•	typed input/output
	•	governed capabilities

Mode B — Wrapped Command Unit

User provides a script, binary, or command and describes how to run it.

Requirements
	•	artifact or command reference
	•	input injection mode
	•	output extraction mode
	•	working directory config
	•	resource limits
	•	governed capabilities

Mode C — Project Unit

User uploads a project/archive and defines build + run behavior.

Requirements
	•	project archive upload
	•	build command
	•	run command
	•	runtime type
	•	artifact target if relevant
	•	input/output modes
	•	dependency/build metadata

Deliverables
	•	onboarding flows
	•	persistence models
	•	runtime adapters per mode

⸻

10.3 Minimal-Adaptation Code Wrapping

Task

Allow existing code to be onboarded with minimal rewrite.

Supported patterns
	•	native structured function
	•	adapter wrapper around existing code
	•	command/script invocation
	•	stdout capture
	•	output file extraction

Product rule

Users must provide a governable envelope, not rewrite all business logic into platform-native abstractions.

Deliverables
	•	wrapper strategy spec
	•	wrapper templates
	•	onboarding helpers

⸻

10.4 Input Injection Modes

Task

Implement supported ways to pass input into executable units.

MVP supported input modes
	•	json_stdin
	•	cli_args
	•	env_vars
	•	input_file_json

Deliverables
	•	injection mode schema
	•	input injector implementations
	•	compatibility tests

⸻

10.5 Output Extraction Modes

Task

Implement supported ways to extract output from executable units.

MVP supported output modes
	•	json_stdout
	•	tagged_stdout_json
	•	output_file_json
	•	text_stdout
	•	exit_code_only

Rules
	•	structured output modes are preferred
	•	unstructured text output is compatibility mode only
	•	output must be converted into typed node output before downstream use

Deliverables
	•	output extraction schema
	•	parsers
	•	extraction validators

⸻

10.6 Language Runtime Strategy

Task

Define language/runtime plugin architecture.

MVP
	•	Python native inline execution
	•	generic command adapter
	•	support wrapping Rust binaries/commands via command or project mode

Future extensibility
	•	JavaScript/TypeScript
	•	richer compiled-runtime support

Deliverables
	•	runtime adapter interface
	•	Python adapter
	•	generic command adapter

⸻

11. Sandbox Execution Module

11.1 Sandbox Manager

Task

Run every executable unit inside a sandbox.

Requirements
	•	isolate process space
	•	isolate filesystem
	•	control network access
	•	control environment variables
	•	enforce timeouts
	•	enforce CPU and memory limits
	•	capture stdout/stderr/logs

Deliverables
	•	sandbox manager
	•	sandbox lifecycle controller
	•	execution wrappers

⸻

11.2 Environment Build and Cache

Task

Build sandbox environments on demand and reuse them safely.

Requirements
	•	build environments based on runtime and dependency/build manifest
	•	cache environments for reuse
	•	cache key must include:
	•	runtime/language
	•	runtime version
	•	dependency manifest hash
	•	build config hash
	•	relevant sandbox/policy identity inputs
	•	cached environments must not violate policy isolation expectations

Deliverables
	•	environment builder
	•	environment cache manager
	•	cache key resolver

⸻

11.3 Policy-Aware Sandbox Enforcement

Task

Integrate policy and capability restrictions into sandbox execution.

Requirements
	•	secrets injected only if allowed
	•	network enabled only if allowed
	•	filesystem access constrained
	•	process spawning constrained by policy
	•	policy violations fail execution and produce audit entries

Deliverables
	•	sandbox policy enforcement layer
	•	secret injection controller
	•	violation reporter

⸻

12. Runtime Orchestration Module

12.1 Async Run Engine

Task

Implement asynchronous graph execution.

Requirements
	•	execute graph from entrypoint
	•	support cyclic traversal
	•	support condition-based branching
	•	support node retries
	•	support pending approval pauses
	•	support async status transitions
	•	persist progress continuously

Deliverables
	•	run orchestrator
	•	node dispatcher
	•	run lifecycle manager

⸻

12.2 Loop and Cycle Safeguards

Task

Prevent infinite or unstable execution.

Requirements
	•	max total graph steps
	•	max total runtime
	•	max visits per node
	•	optional max visits per edge
	•	explicit loop termination reason
	•	stuck-run diagnostics
	•	cycle state recorded in run state

Deliverables
	•	loop guard
	•	termination policy engine
	•	diagnostics recorder

⸻

12.3 Run State Persistence

Task

Persist graph execution state.

Requirements

Run state must track:
	•	current status
	•	optional thread ID
	•	visited nodes
	•	pending nodes
	•	node attempt counts
	•	condition evaluations
	•	approval waits
	•	audit record references
	•	final result or terminal error

Deliverables
	•	run state schema
	•	persistence repository
	•	state transition manager

⸻

13. Thread and State Persistence Module

13.1 Thread Lifecycle

Task

Implement thread creation, continuation, and lookup semantics.

Requirements
	•	a run may create a new thread or continue an existing thread
	•	thread identity must be externally referenceable as thread_id
	•	thread lookup must occur before relevant agent execution
	•	thread lifecycle must be independent from run lifecycle

Deliverables
	•	thread model
	•	thread repository
	•	thread resolver

⸻

13.2 Agent State Checkpointing

Task

Persist and restore agent state via thread_id.

Requirements
	•	restore thread-scoped state before stateful agent execution
	•	checkpoint updated state after stateful agent execution
	•	support no-op behavior for stateless agents
	•	separate thread checkpoints from node-local audit records

Deliverables
	•	state checkpoint interface
	•	thread-scoped state store
	•	checkpoint/restore hooks

⸻

13.3 Thread-Aware Invocation Semantics

Task

Integrate thread_id into public invocation and internal runtime semantics.

Requirements
	•	invocation may omit thread_id to create new thread context when applicable
	•	invocation may include thread_id to continue prior stateful execution context
	•	thread linkage must be visible in run state and node audits where relevant

Deliverables
	•	invocation contract updates
	•	thread-aware runtime resolver
	•	thread linkage serializer

⸻
14. Human Interaction and Approval Module

14.1 Human Approval Node Runtime

Task

Implement human approval nodes as first-class pausing steps in graph execution.

Requirements
	•	accept structured input from upstream node output after edge mapping and validation
	•	create a human-interaction record linked to:
	•	run_id
	•	thread_id if present
	•	node_id
	•	relevant deployment and graph version references
	•	persist paused execution state
	•	generate a structured human-facing approval payload rather than exposing raw internal state
	•	support interaction type approval in MVP
	•	allow the following decision modes:
	•	approve
	•	reject
	•	edit_and_approve
	•	support optional edited payload submission when permitted by node policy
	•	validate edited payload against the approval resolution schema before continuation
	•	resume execution asynchronously after resolution
	•	emit structured output on resolution for downstream node consumption
	•	record approval decision, actor, timestamp, and decision metadata in node-local audit history
	•	preserve linkage between approval event, paused run state, and continuation event

Resolution semantics
	•	approve continues execution with approved output payload
	•	reject terminates the current branch or run according to configured failure/termination policy
	•	edit_and_approve continues execution with validated edited payload
	•	unresolved approval leaves the run in paused_for_approval

Deliverables
	•	approval node runner
	•	approval interaction model
	•	paused execution state model
	•	approval resolution validator
	•	approval event integration
	•	audit integration for approval lifecycle

⸻

14.2 Approval API

Task

Allow paused human-approval interactions to be inspected and resolved through an explicit API surface.

Requirements
	•	fetch pending approval interactions by:
	•	approval ID
	•	run ID
	•	thread ID where applicable
	•	deployment or graph scope where appropriate
	•	fetch structured approval context including:
	•	summary
	•	rationale for approval request
	•	requested action
	•	allowed response modes
	•	relevant structured context excerpt
	•	originating node metadata
	•	fetch related node audit context in a redacted, human-consumable form
	•	submit approval decision as one of:
	•	approve
	•	reject
	•	edit_and_approve
	•	submit edited payload when allowed by approval node policy
	•	validate approver identity and authorization before accepting decision
	•	validate edited payload against the resolution schema
	•	trigger asynchronous continuation of the paused run after valid resolution
	•	return updated approval state and linked run state after resolution
	•	prevent duplicate or conflicting resolutions for the same pending approval
	•	record all approval API actions in audit history

Deliverables
	•	approval query API
	•	approval resolution API
	•	approval payload schema
	•	approval response schema
	•	approver authorization check
	•	continuation trigger
	•	idempotency and conflict-handling logic

⸻

14.3 Human-Facing Approval Payload Contract

Task

Define a structured payload contract for approval requests so human interactions are readable, governed, and channel-portable.

Requirements
	•	payload must include:
	•	approval ID
	•	run_id
	•	optional thread_id
	•	originating node ID
	•	interaction type
	•	short summary
	•	rationale
	•	requested decision
	•	allowed actions
	•	relevant context excerpt
	•	optional proposed payload for approval
	•	urgency or priority metadata if configured
	•	payload must exclude secrets and restricted data according to redaction policy
	•	payload must be renderable in internal UI and reusable later for channel adapters such as email or Slack
	•	payload structure must remain separate from internal full audit data

Deliverables
	•	approval payload contract
	•	human-readable payload formatter
	•	redaction-aware approval serializer

⸻

14.4 Future Extension Hooks

Task

Preserve extensibility for broader human-interaction patterns beyond approval without expanding MVP scope.

Requirements
	•	internal model should reserve support for future interaction types:
	•	clarification
	•	request_input
	•	notification
	•	approval implementation must not hardcode assumptions that all human interactions are binary approvals
	•	runtime and API design should allow future addition of human endpoint routing and channel adapters without breaking approval semantics

Deliverables
	•	extensible human interaction type enum
	•	interaction-type-safe internal model
	•	forward-compatible API design
	
⸻

15. Audit Module

15.1 Per-Node Audit Storage

Task

Implement node-local execution audit records.

Requirements

Each node execution attempt must record:
	•	run_id
	•	optional thread_id
	•	mapped input snapshot or redacted form
	•	validation results
	•	execution metadata
	•	output snapshot or redacted form
	•	errors if any
	•	condition results if relevant
	•	memory interactions if agent
	•	tool calls if agent
	•	stdout/stderr if executable unit
	•	approval actions if approval node

Deliverables
	•	node audit schema
	•	audit repository
	•	write pipeline

⸻

15.2 Audit Query Interfaces

Task

Allow audits to be queried by different scopes.

Required query scopes
	•	by node
	•	by run
	•	by graph version
	•	by deployment
	•	by thread where relevant

Important

Aggregation is allowed, but underlying ownership stays node-local.

Deliverables
	•	audit read API
	•	filtering/query layer
	•	audit timeline assembler

⸻

15.3 Redaction and Sensitive Data Handling

Task

Control what is stored in audit payloads.

Requirements
	•	support redaction rules
	•	allow secret masking
	•	allow selective omission of payload fields
	•	preserve enough observability for debugging

Deliverables
	•	audit redaction policy layer
	•	payload sanitizer
	•	redaction config model

⸻

16. Policy and Capability Module

16.1 Capability Declarations

Task

Declare and enforce explicit capabilities for executable units and agent attachments.

MVP capability set
	•	network_read
	•	network_write
	•	filesystem_read
	•	filesystem_write
	•	secret_access
	•	external_api_call
	•	process_spawn
	•	memory_read
	•	memory_write

Deliverables
	•	capability schema
	•	manifest bindings
	•	runtime enforcement hooks

⸻

16.2 Policy Model

Task

Implement graph-level and node-level policies.

MVP policy types
	•	allowed capabilities
	•	denied capabilities
	•	secret allowlist
	•	network mode
	•	approval required before side effects
	•	timeout overrides
	•	sandbox strictness mode

Deliverables
	•	policy schema
	•	policy binding model
	•	policy evaluation module

⸻

16.3 Runtime Enforcement

Task

Apply policies during execution.

Requirements
	•	policies must be evaluated before node execution
	•	sandbox behavior must honor policies
	•	capability mismatches must fail execution
	•	policy rejections must produce node-local audit entries

Deliverables
	•	runtime policy guard
	•	enforcement result model
	•	audit integration

⸻

17. Deployment Module

17.1 Published Graph Deployment

Task

Deploy only immutable published graph versions.

Requirements
	•	published versions immutable
	•	deployment references fixed graph version
	•	deployment captures bound manifests/configs/policies
	•	rollback supported by redeploying earlier published version

Deliverables
	•	deployment model
	•	publish-to-deploy workflow
	•	rollback mechanism

⸻

17.2 Service Wrapper

Task

Expose deployed graph through a service wrapper.

Wrapper responsibilities
	•	receive invocation requests
	•	create async run
	•	accept optional thread_id
	•	expose run status retrieval
	•	expose input/output contract metadata
	•	expose approval resolution routes if needed
	•	expose health endpoint

Deliverables
	•	wrapper runtime
	•	deployment bootstrapper
	•	API handler layer

⸻

17.3 Public API Contract Exposure

Task

Expose service-facing schema metadata.

Requirements
	•	input contract endpoint
	•	output contract endpoint
	•	error/result state schema
	•	deployment version metadata

Deliverables
	•	contract exposure API
	•	schema serialization layer
	•	service metadata endpoint

⸻

18. Async Invocation and Run API Module

18.1 Run Creation API

Task

Create async runs for deployed graphs.

Requirements
	•	accept typed input payload
	•	accept optional thread_id
	•	validate against deployment input contract
	•	create run record
	•	enqueue or dispatch execution
	•	return run_id, initial status, and effective thread linkage

Deliverables
	•	invocation API
	•	request validator
	•	run creation service

⸻

18.2 Run Status API

Task

Allow clients to inspect run state.

Requirements
	•	return current run status
	•	return terminal output if available
	•	return terminal error if failed
	•	return approval-required state if paused
	•	support audit references or summaries
	•	return thread linkage where relevant

Deliverables
	•	run status API
	•	response schemas
	•	run lookup service

⸻

18.3 Run Result Shapes

Task

Define public result state models.

Required states
	•	queued
	•	running
	•	paused_for_approval
	•	succeeded
	•	failed
	•	terminated_by_policy
	•	terminated_by_loop_guard

Deliverables
	•	result status schema
	•	terminal/nonterminal response models
	•	API documentation

⸻

19. N8n-Alignment Constraints

The platform should be structurally aligned with n8n in how users think about authoring, but not in product purpose.

Required alignment principles
	•	graph is the main mental model
	•	node-centric configuration
	•	connector-style optional attachments
	•	composable graph editing
	•	visually manageable flow semantics
	•	shared resource instances possible, such as memory connectors

Explicit difference from n8n
	•	stronger typed contracts
	•	stronger execution governance
	•	stronger sandboxing
	•	stronger auditability
	•	first-class agent nodes
	•	thread-based state persistence
	•	production-oriented API deployment target

⸻

20. MVP Deliverables Summary

The MVP must include:

Graph layer
	•	graph CRUD
	•	graph versioning
	•	graph validation
	•	cyclic graph support
	•	condition subsystem

Contracts
	•	Pydantic-style contract registry
	•	runtime validation
	•	edge mappings

Agents
	•	agent nodes
	•	model invocation
	•	tool attachment
	•	optional memory connectors
	•	shared memory instance semantics
	•	optional thread-based state restore/checkpoint behavior

Executable units
	•	strict manifest
	•	native unit mode
	•	wrapped command unit mode
	•	project unit mode
	•	minimal-adaptation wrapping
	•	structured input/output modes

Sandboxing
	•	per-unit sandbox execution
	•	environment build on demand
	•	cached environment reuse
	•	policy-aware sandboxing

Runtime
	•	async orchestration
	•	loop safeguards
	•	run persistence
	•	condition-based branching
	•	retries and termination behavior

Thread persistence
	•	thread model
	•	thread-aware invocation
	•	agent state checkpointing keyed by thread_id

Human control
	•	approval node
	•	approval API
	•	async continuation

Audits
	•	per-node audit logs
	•	audit querying
	•	redaction controls
	•	thread linkage where relevant

Governance
	•	capability declarations
	•	policy bindings
	•	runtime enforcement

Deployment
	•	immutable published deployment
	•	service wrapper
	•	async invocation API
	•	run status API
	•	contract exposure API

⸻

21. Recommended Build Order

Phase 1 — Core foundation
	•	canonical domain models
	•	graph schema
	•	contract registry
	•	edge mappings
	•	graph validation
	•	run state model
	•	thread model

Phase 2 — Execution core
	•	agent runner
	•	executable unit manifest
	•	sandbox manager
	•	environment cache
	•	runtime orchestrator
	•	condition evaluator
	•	thread checkpoint/restore hooks

Phase 3 — Platform control
	•	memory connectors
	•	approval node
	•	audit system
	•	policy/capability enforcement

Phase 4 — Deployment surface
	•	publish/deploy flow
	•	service wrapper
	•	async invocation/status APIs
	•	contract exposure
	•	thread-aware external API semantics

⸻

22. Immediate Next Artifacts To Write

The next documents that should be written in detail are:

22.1 Executable Unit Manifest Spec

This is the most important next artifact.

It should fully define:
	•	onboarding modes
	•	artifact sources
	•	build/run configs
	•	input/output strategies
	•	capability requests
	•	sandbox requirements
	•	cache identity model

22.2 Runtime Execution Semantics Spec

It should define:
	•	node scheduling
	•	condition evaluation order
	•	cycle traversal rules
	•	retry order
	•	approval pause/resume behavior
	•	terminal state rules
	•	thread restore/checkpoint timing

22.3 Thread and State Persistence Spec

It should define:
	•	relation between run_id and thread_id
	•	when thread state is loaded
	•	when thread state is checkpointed
	•	how stateless vs stateful agents differ
	•	how shared memory connectors interact with thread persistence
	•	how retries affect thread state

22.4 Public API Spec

It should define:
	•	invoke endpoint
	•	run status endpoint
	•	approval resolution endpoint
	•	health endpoint
	•	schema exposure endpoints
	•	optional thread_id semantics

22.5 Audit Record Spec

It should define:
	•	exact audit schemas by node type
	•	redaction behavior
	•	retention defaults
	•	query shapes
	•	thread linkage fields

⸻

23. One-Sentence Engineering Directive

Build a graph-native, async, policy-governed modular monolith where agent nodes, executable units, human approval nodes, and conditions compose typed cyclic workflows, executable units run in cached sandboxes, agent memory is connector-based and shareable, stateful agent continuity is keyed by thread_id, audits are node-local, and published graphs are exposed through a standalone API service wrapper.