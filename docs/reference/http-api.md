# HTTP API Reference

Interactive reference for the `zeroth-core` FastAPI service, rendered from the committed OpenAPI spec at [`openapi/zeroth-core-openapi.json`](https://github.com/rrrozhd/zeroth-core/blob/main/openapi/zeroth-core-openapi.json). The spec is regenerated from the FastAPI app on every commit via `scripts/dump_openapi.py`, and CI fails if it drifts.

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

## Regenerating the spec

The spec is a committed snapshot, not fetched live. To refresh it locally:

```bash
uv run python scripts/dump_openapi.py --out openapi/zeroth-core-openapi.json
cp openapi/zeroth-core-openapi.json docs/assets/openapi/zeroth-core-openapi.json
```

CI runs `python scripts/dump_openapi.py --check --out openapi/zeroth-core-openapi.json` on every PR and fails if the committed snapshot is stale.

## Offline consumption

The raw JSON is available at [`/assets/openapi/zeroth-core-openapi.json`](../assets/openapi/zeroth-core-openapi.json) for tooling that wants to consume it directly (e.g., `openapi-typescript`, Postman import, ReDoc).
