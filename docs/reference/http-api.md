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
      url: "../assets/openapi/zeroth-core-openapi.json",
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
