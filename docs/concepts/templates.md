# Templates

*Added in v4.0*

The template registry stores and versions prompt templates by name, enabling teams to manage prompt content separately from graph structure. Templates support Jinja2 variable interpolation in a sandboxed environment that prevents injection attacks. Secret variables are automatically identified and redacted in audit records.

## How It Works

Templates are registered with a name, version, and content string. Agent nodes reference a template by name (and optionally version) via a `TemplateReference` on their configuration. At runtime, the `TemplateRenderer` resolves the template, renders it with the node's input variables using Jinja2's `SandboxedEnvironment`, and passes the rendered prompt to the LLM. Variables matching known secret patterns (API keys, tokens, passwords) are automatically redacted in audit records via the redaction module.

## Key Components

- **`TemplateRegistry`** -- In-memory registry storing `PromptTemplate` objects by name and version. Supports create, get (by name or name+version), list, and delete operations. Constructed during `bootstrap_service()`.
- **`TemplateRenderer`** -- Renders a template string with variables using Jinja2's `SandboxedEnvironment`. Prevents arbitrary code execution in template expressions. Returns a `TemplateRenderResult` with the rendered text and metadata.
- **`PromptTemplate`** -- Pydantic model representing a stored template with name, version, content, and metadata fields.
- **`TemplateReference`** -- Lightweight reference (name + optional version) used by agent nodes to point to a template without embedding its content.

## REST API

- `GET /v1/templates` -- List all registered templates. Requires `run:read` permission.
- `POST /v1/templates` -- Register a new template (name, version, content). Requires `admin` permission. Returns 409 if the name+version already exists.
- `GET /v1/templates/{name}` -- Get a template by name (latest version). Optional `?version=N` query parameter for a specific version. Requires `run:read` permission.
- `DELETE /v1/templates/{name}/{version}` -- Remove a specific template version. Requires `admin` permission.

## Secret Redaction

The `redact_rendered_prompt()` function scans rendered output for values matching `DEFAULT_SECRET_PATTERNS` (API keys, bearer tokens, passwords) and replaces them with `[REDACTED]` before writing to audit records. The `identify_secret_variables()` function inspects variable names to flag likely secrets before rendering.

## Error Handling

- **`TemplateNotFoundError`** -- Raised when a referenced template name or version does not exist.
- **`TemplateVersionExistsError`** -- Raised when registering a duplicate name+version combination (409 via REST).
- **`TemplateRenderError`** -- Raised when Jinja2 rendering fails (missing variables, syntax errors).
- **`TemplateSyntaxValidationError`** -- Raised during template registration if the Jinja2 syntax is invalid.

## Configuration

The template registry is configured via `bootstrap_service()`. If no registry is configured, REST endpoints return 503.

See the [API Reference](../reference/http-api.md) for endpoint details and the source code under `zeroth.core.templates` for implementation.
