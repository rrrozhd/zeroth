# Sandbox container

`zeroth-core` ships as a Python library, not a runnable container. When you
deploy, you build your own image (or compose stack). This page shows the
minimal recipe for wiring up the **sandbox sidecar** — the isolated container
backend `zeroth.core` uses to run untrusted executable units.

The library does not assume any particular orchestrator. Docker, Podman,
Kubernetes, or a bare `docker run` all work: the runtime only invokes a
binary that speaks the Docker CLI protocol and inspects whether a named
container is running.

## When you need this

You only need a sandbox container if your graphs include **executable
units** that run untrusted code. If you stick to agent nodes and native
tools you can skip this page.

## Settings

Configure the sandbox via `DockerSandboxSettings` (see
`zeroth.core.config.settings`):

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `backend` | `ZEROTH_SANDBOX__BACKEND` | `local` | `local`, `docker`, `sidecar`, or `auto` |
| `docker_container_name` | `ZEROTH_SANDBOX__DOCKER_CONTAINER_NAME` | `zeroth-sandbox` | Name of the long-running sandbox container |
| `docker_binary` | `ZEROTH_SANDBOX__DOCKER_BINARY` | `docker` | CLI to invoke (e.g. `podman`) |

Set `backend=docker` (or `sidecar` if you prefer the HTTP sidecar app at
`zeroth.core.sandbox_sidecar`) and make sure the named container is
running and reachable from the `zeroth-core` process.

## Minimal recipe

Any image that provides a Python runtime and the dependencies your
executable units need will work. A starting point:

```dockerfile
FROM python:3.12-slim

RUN useradd -m -u 10001 sandbox
USER sandbox
WORKDIR /work

# Pin only what you need. Add numpy, pandas, etc. as required.
RUN pip install --no-cache-dir --user httpx
```

Run it named to match `docker_container_name`, with network and filesystem
locked down as tightly as your threat model requires:

```bash
docker run -d \
  --name zeroth-sandbox \
  --read-only \
  --network none \
  --memory 512m \
  --cpus 1.0 \
  --tmpfs /tmp:rw,size=64m \
  your-sandbox-image:latest sleep infinity
```

`zeroth-core` will exec into this container for each unit and apply
per-execution resource flags (`build_docker_resource_flags` in
`zeroth.core.execution_units.constraints`) on top of your baseline.

## Production notes

- **Image immutability**: pin by digest, not tag, and rebuild on a
  regular cadence to pick up CVE fixes in the base image.
- **Network**: start with `--network none`; open only what specific
  executable units need, ideally via a dedicated egress proxy.
- **Filesystem**: `--read-only` plus a sized tmpfs eliminates an entire
  class of persistence attacks.
- **Resource limits**: set `--memory`, `--cpus`, and `--pids-limit` at
  the container level; per-unit caps compose with these.
- **Orchestration**: on Kubernetes, run the sandbox as a sidecar pod or
  a dedicated node pool with the same hardening. The library only needs
  a reachable `docker`-compatible CLI on the `zeroth-core` host.

## Health check

The runtime calls `docker inspect -f '{{.State.Running}}'` to decide if
the sidecar is healthy (`docker_container_running` in
`zeroth.core.storage.redis` and equivalent checks in the sandbox
module). Your orchestrator's own health probes are independent — use
both.
