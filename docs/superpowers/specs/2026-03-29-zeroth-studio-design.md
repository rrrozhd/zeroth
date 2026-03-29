# Zeroth Studio Design

Date: 2026-03-29
Topic: Studio authoring shell, navigation model, and UX boundaries

## Summary

Zeroth Studio should be a canvas-first authoring product with a quiet, minimal default shell. The center of gravity is workflow editing, not operations. Runtime, governance, and deployment controls remain close at hand, but they appear progressively instead of dominating the interface.

The intended product feel is similar to n8n's editor posture: a persistent left workflow rail, a central canvas, a contextual inspector, and a mode switch that moves between authoring and runtime-oriented views. The key difference is Zeroth-specific semantics and governance depth: agents, executable units, memory resources, environments, approvals, audits, evidence, and attestation are surfaced without turning the default experience into an operations dashboard.

## Product Goals

- Make workflow authoring the unmistakable primary activity.
- Keep the interface minimal by default, even though the product is technically advanced.
- Preserve clear access to runtime, governance, and deployment detail.
- Separate reusable building blocks from workflow-specific instances without forcing users into a detached asset-management workflow.
- Keep environment and secret management explicit and cross-cutting, not mixed into routine authoring navigation.

## Shell Architecture

### Default Posture

The product opens directly into a workflow editor. The first screen should communicate that Zeroth Studio is for building and iterating on workflows, not primarily for monitoring operations.

### Left Rail

The left rail is reserved for workflows only.

- Workflows are grouped under folders.
- The visual tone should stay quiet and list-oriented, similar to a thread/work item navigator rather than a dense application menu.
- The rail's job is fast workflow switching and orientation, not exposing every system object.

At the lower part of the rail, there is one secondary entry: `Assets`.

### Center Workspace

The center workspace is the canvas and remains visually dominant. This is where structural workflow editing happens:

- add and arrange nodes
- connect nodes
- author conditions and branching
- inspect workflow structure at a glance

### Top Mode Switch

Above the canvas sits a compact mode switch:

- `Editor`
- `Executions`
- `Tests`

This keeps major context changes explicit and easy to scan while preserving a strong common shell across all modes.

### Right Inspector

The right side is a contextual inspector. It should stay narrow, focused, and calm.

Its primary job is node authoring, not acting as a second application surface.

### Header

The header carries cross-cutting controls:

- save state
- current environment dropdown
- publish/deploy actions
- workflow title and lightweight status

Environment control should be a simple current-environment dropdown with a `Manage environments` entry. This is the most scalable and least visually noisy option.

## Authoring Model

### Workflow Nodes Versus Reusable Assets

In the authoring experience, agents, executable units, and memory resources behave like nodes on the canvas. However, each node is typically an instance of a reusable definition.

The UX distinction is:

- the canvas shows workflow-specific instances
- `Assets` exposes the reusable definitions behind those instances

This applies to:

- Agents
- Executable Units
- Memory Resources

### Assets Surface

`Assets` should open as a slide-over or side-panel by default rather than as a full-screen mode.

Reasoning:

- users usually open assets in service of the workflow they are already editing
- preserving canvas context is more important than maximizing asset browsing space
- it reinforces that assets support authoring instead of replacing it

When the user needs deeper work such as detailed editing, version inspection, or file-level manipulation, the product can escalate into a fuller dedicated asset editor.

### Contracts

Contracts are not first-class assets in the Studio UX.

Although backend persistence can continue to reuse shared contract-registry infrastructure, the user-facing authoring model should treat contracts as part of node configuration and connection/mapping work.

Contracts should therefore be authored and inspected from:

- node inspector context
- edge mapping UI
- node-specific configuration surfaces

This keeps contracts close to the place where users actually reason about them.

### Environments

Environments are first-class workspace resources, but they should not live in `Assets` or the left rail.

They belong in the header/settings layer because they hold cross-cutting operational context such as:

- API keys
- secrets
- passwords
- test versus production bindings
- environment-specific configuration for agents and executable units

The header environment dropdown should support fast switching, while deeper setup lives under `Manage environments`.

## Runtime And Governance Model

### Core Principle

Runtime and governance data should be present, but ambient. The editor must not feel like a heavy operations console by default.

### Executions View

`Executions` should use a mixed model.

The default view is a run-level timeline, but the same underlying event data must also be reachable in node-scoped context.

This means approvals, audits, evidence, attestation, and related runtime records should support two pivots:

- by run
- by node

This is preferable to a design where all governance data lives in one detached page or one flat tab structure.

### Node-Scoped Runtime Access

When users are in `Editor`, node runtime detail should not overwhelm the authoring flow.

Clicking a node should open:

- node configuration
- a compact recent-activity summary for that node

The inspector should expose a clear path into deeper node history, but it should not inline the full execution/audit experience by default.

This creates a clean escalation path:

- quick context in the inspector
- deeper node history from explicit navigation
- full run narrative in `Executions`

### Ambient Runtime Presence

The leading visual direction is a contextual-runtime shell.

That means runtime visibility exists through subtle signals:

- recent activity summaries
- lightweight node status
- clear entry points to run and node timelines

But the surface still reads first as an editor.

## Interaction Rules

### Editor Mode

Editor mode is for structural authoring and focused configuration.

Expected behaviors:

- structural canvas edits are central
- the inspector is contextual and selection-driven
- governance depth is discoverable but not constantly expanded

### Tests Mode

Tests mode should run against the latest persisted authoring snapshot, not unsaved transient state. This keeps test behavior explainable and reduces ambiguity.

### Save Model

The save model is intentionally split:

- structural canvas edits autosave
- advanced settings and metadata require explicit save

This protects fast authoring while preserving control over higher-risk or less frequently changed settings.

### Validation

Validation should feel immediate and local:

- connection/type issues near relevant nodes and edges
- environment/configuration gaps in inspector context
- compile/publish blockers summarized before deployment

The user should be able to tell quickly:

- what failed
- where it failed
- what to do next

## Information Architecture Summary

### Primary Navigation

- Left rail: workflows only
- Bottom rail entry: `Assets`
- Header: environment dropdown, save/deploy controls
- Top mode switch: `Editor`, `Executions`, `Tests`

### Reusable Definitions

`Assets` contains:

- Agents
- Executable Units
- Memory Resources

### Contextual Rather Than Primary

- Contracts: node-local/configuration-local concern
- Environments: header/settings concern

## UX Recommendations

### Recommended Shell

The strongest direction is:

- canvas-first home
- quiet workflow rail
- contextual-runtime feel
- top mode switch
- right inspector with config plus compact node activity

### Recommended Complexity Strategy

Use progressive disclosure everywhere:

- minimal everyday shell
- richer detail on explicit interaction
- deep governance records only when intentionally opened

This matches the product's technical depth without forcing that depth into the user's face at all times.

## Open Questions For Planning

- Exact structure of the `Assets` slide-over: whether it opens grouped by asset type, search-first, or "used in this workflow" priority.
- Exact visual styling and density of the workflow rail and contextual inspector.
- Precise relationship between `Executions` and `Tests` when draft runs and deployed runs appear in the same overall shell.
- Whether deeper asset editing should happen in-place, in a dedicated page, or in a layered panel pattern per asset type.

## Recommended Next Step

Translate this design into an implementation plan that separates:

- studio backend authoring/gateway responsibilities
- frontend shell and routing
- canvas and inspector foundations
- executions and tests views
- assets and environment management
- compiler/gateway integration points with the existing runtime surfaces
