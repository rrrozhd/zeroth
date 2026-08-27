"""Deterministic loopback peer for the provider-independent HTTP-node journey.

The service retains only closed scenario names, sequence numbers, and response
statuses. It accepts no arbitrary destination, headers, credentials, request
body, or response content, so its event stream is safe to ingest as campaign
evidence.  It must bind to loopback; the CLI rejects every broader interface.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from fastapi import FastAPI, Response


@dataclass(slots=True)
class HttpScenarioState:
    """Campaign-local deterministic peer state."""

    retry_failures: int = 2
    timeout_seconds: float = 0.25
    recovered: bool = False
    retry_attempts: int = 0
    sequence: int = 0
    events: list[dict[str, int | str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if not 0 <= self.retry_failures <= 10:
            raise ValueError("retry_failures must be between 0 and 10")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be positive and at most 30")

    async def record(self, scenario: str, status_code: int) -> int:
        async with self.lock:
            self.sequence += 1
            self.events.append(
                {
                    "sequence": self.sequence,
                    "scenario": scenario,
                    "status_code": status_code,
                }
            )
            return self.sequence

    async def retry_status(self) -> tuple[int, int]:
        async with self.lock:
            self.retry_attempts += 1
            status = 503 if self.retry_attempts <= self.retry_failures else 200
            self.sequence += 1
            self.events.append(
                {
                    "sequence": self.sequence,
                    "scenario": "retry-then-success",
                    "status_code": status,
                }
            )
            return status, self.retry_attempts

    async def reset(self) -> None:
        async with self.lock:
            self.recovered = False
            self.retry_attempts = 0
            self.sequence = 0
            self.events.clear()


def validate_loopback_bind_host(host: str) -> str:
    """Reject an accidentally network-visible fault service."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("resilient HTTP scenario server must bind to loopback")
    return host


def create_http_scenario_app(state: HttpScenarioState | None = None) -> FastAPI:
    runtime = state or HttpScenarioState()
    app = FastAPI(title="Zeroth resilient HTTP scenario peer", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/scenario/retry-then-success")
    async def retry_then_success() -> Response:
        status_code, attempt = await runtime.retry_status()
        return Response(
            content=f'{{"scenario":"retry-then-success","attempt":{attempt}}}',
            status_code=status_code,
            media_type="application/json",
        )

    @app.get("/scenario/timeout")
    async def timeout() -> dict[str, str]:
        await asyncio.sleep(runtime.timeout_seconds)
        await runtime.record("timeout", 200)
        return {"scenario": "timeout"}

    @app.get("/scenario/circuit")
    async def circuit() -> Response:
        status_code = 200 if runtime.recovered else 503
        await runtime.record("circuit", status_code)
        return Response(
            content=f'{{"scenario":"circuit","recovered":{str(runtime.recovered).lower()}}}',
            status_code=status_code,
            media_type="application/json",
        )

    @app.post("/control/recover", status_code=204)
    async def recover() -> Response:
        async with runtime.lock:
            runtime.recovered = True
        return Response(status_code=204)

    @app.post("/control/reset", status_code=204)
    async def reset() -> Response:
        await runtime.reset()
        return Response(status_code=204)

    @app.get("/control/events")
    async def events() -> dict[str, object]:
        async with runtime.lock:
            return {
                "events": [dict(event) for event in runtime.events],
                "recovered": runtime.recovered,
            }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--retry-failures", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=0.25)
    args = parser.parse_args()
    host = validate_loopback_bind_host(args.host)
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be between 1 and 65535")

    import uvicorn

    uvicorn.run(
        create_http_scenario_app(
            HttpScenarioState(
                retry_failures=args.retry_failures,
                timeout_seconds=args.timeout_seconds,
            )
        ),
        host=host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
