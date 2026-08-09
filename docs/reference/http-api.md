# HTTP API Reference

Interactive reference for the `zeroth-core` FastAPI service. The OpenAPI
spec is generated from the FastAPI app at docs-build time via
`scripts/dump_openapi.py` — it is not committed to the repo.

<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui.css" />
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5.17.14/swagger-ui-bundle.js" charset="UTF-8"></script>
<script>
  window.addEventListener("load", function () {
    window.ui = SwaggerUIBundle({
      // Substituted at build time by scripts/mkdocs_hooks.py. MkDocs rewrites relative
      // URLs in Markdown but not inside a <script>, and with directory URLs this page is
      // served from /reference/http-api/ -- one level deeper than its source path -- so
      // any URL written by hand here is wrong in at least one URL mode (ZER-20). The
      // build computes it with MkDocs' own path logic and fails if this token is gone.
      url: "@@ZEROTH_OPENAPI_SPEC_URL@@",
      dom_id: "#swagger-ui",
      deepLinking: true,
      presets: [SwaggerUIBundle.presets.apis],
      layout: "BaseLayout",
    });
  });
</script>

## Regenerating the spec locally

```bash
uv run python scripts/dump_openapi.py --out docs/assets/openapi/zeroth-core-openapi.json
```

The docs CI runs the same command before `mkdocs build`, so the
published Swagger UI always reflects the live FastAPI routes.

## Offline consumption

The raw JSON is served at [`/assets/openapi/zeroth-core-openapi.json`](../assets/openapi/zeroth-core-openapi.json) on the built docs site for tooling that wants to consume it directly (e.g., `openapi-typescript`, Postman import, ReDoc).
