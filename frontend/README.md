# Zeroth Console (frontend)

A Next.js **static export** console for operating and authoring Zeroth apps.
It talks to the `zeroth-core` HTTP API and runs in two modes from one bundle:
mounted by the Zeroth app at `/console`, or hosted standalone. See the
[Web Console section of the root README](../README.md#web-console) for the
deploy-mode overview.

> **Note (Next 16):** this project pins specific conventions that differ from
> older Next.js. Read `node_modules/next/dist/docs/` before changing build/
> routing config. Key constraints we rely on:
> - `output: "export"` → a client-only SPA. No SSR, server actions, route
>   handlers, or middleware (they don't exist in the export).
> - `basePath: "/console"` → one mount subpath shared by both modes.
> - Detail views read IDs from **query params**, not `[id]` routes (dynamic
>   routes 404 under static export).

## Develop

```bash
npm install
npm run dev      # http://localhost:3000/console/
```

Standalone dev needs CORS on the API. Start your Zeroth service with:

```bash
export ZEROTH_CONSOLE_CORS_ORIGINS="http://localhost:3000"
```

Then in the console's *Connect* bar set the API base URL (e.g.
`http://127.0.0.1:8000`) and an `X-API-Key`. Both persist to localStorage.

## Build

```bash
npm run build    # produces ./out (static export)
```

Any Zeroth service auto-mounts `./out` at `/console` when present; override the
location with `ZEROTH_CONSOLE_DIR`.

## Layout

```
app/
  layout.tsx            # shell + metadata (system fonts; no build-time fetch)
  page.tsx              # console home (client component)
  components/           # UI components (e.g. ConnectBar)
  lib/
    config.ts           # runtime API base + key (localStorage)
    api.ts              # typed fetch wrapper + endpoint helpers
```

Runtime config lives in `lib/config.ts`: the API base defaults to the current
origin (mounted mode) and is overridable for standalone mode. Every request
carries `X-API-Key` from `lib/config.ts`.
