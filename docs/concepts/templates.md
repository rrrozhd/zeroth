# Templates

The template registry stores immutable prompt-template versions by tenant,
workspace, name, and version, enabling teams to manage prompt content separately
from graph structure. Templates support Jinja2 interpolation in a sandboxed
environment that blocks unsafe template-language operations. This sandbox is
not a general defense against prompt injection in rendered text. Secret-named
variables are redacted before rendered prompts enter audit records.

## How It Works

Templates are registered with a name, version, and content string. Agent nodes reference a template by name (and optionally version) via a `TemplateReference` on their configuration. At runtime, the `TemplateRenderer` resolves the template, renders it with the node's input variables using Jinja2's `SandboxedEnvironment`, and passes the rendered prompt to the LLM. Variables matching known secret patterns (API keys, tokens, passwords) are automatically redacted in audit records via the redaction module.

## Key Components

- **`DatabaseTemplateRegistry`** -- The service registry. It stores immutable
  `PromptTemplate` versions in the service database under the authenticated
  tenant and workspace scope. An in-memory `TemplateRegistry` remains available
  for tests and embedded callers.
- **`TemplateRenderer`** -- Renders a template string with variables using Jinja2's `SandboxedEnvironment`. Prevents arbitrary code execution in template expressions. Returns a `TemplateRenderResult` with the rendered text and metadata.
- **`PromptTemplate`** -- Pydantic model representing a stored template with name, version, content, and metadata fields.
- **`TemplateReference`** -- Lightweight reference (name + optional version) used by agent nodes to point to a template without embedding its content.

## REST API

- `GET /v1/templates` -- List all registered templates. Requires `run:read` permission.
- `POST /v1/templates` -- Register a new template (name, version, content). Requires `template:admin` permission. Returns 409 if the name+version already exists.
- `GET /v1/templates/{name}` -- Get a template by name (latest version). Optional `?version=N` query parameter for a specific version. Requires `run:read` permission.
- `DELETE /v1/templates/{name}/{version}` -- Remove a specific template version. Requires `template:admin` permission.

## Secret Redaction

`identify_secret_variables()` classifies variable paths whose names contain a
configured secret pattern such as `api_key`, `token`, or `password`.
`redact_rendered_prompt()` then replaces those values in the rendered audit copy
with `***REDACTED***`. Runtime dispatch recursively discovers nested mapping,
list, and tuple paths before classification. Redaction protects the audit copy;
the provider still receives the rendered prompt required by the workflow.

## Error Handling

- **`TemplateNotFoundError`** -- Raised when a referenced template name or version does not exist.
- **`TemplateVersionExistsError`** -- Raised when registering a duplicate name+version combination (409 via REST).
- **`TemplateRenderError`** -- Raised when Jinja2 rendering fails (missing variables, syntax errors).
- **`TemplateSyntaxValidationError`** -- Raised during template registration if the Jinja2 syntax is invalid.

## Configuration

The scoped database registry is configured during service bootstrap. If no
registry is configured, REST endpoints return 503.

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.contracts.templates` for implementation.
