"""Daemon workload ownership around the SDK's attached Docker stdio client."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from zeroth.platform.primitives import cleanup_despite_cancellation


@dataclass(frozen=True)
class DockerStdioWorkload:
    """Private runtime metadata minted by the operator-owned isolator."""

    docker_binary: str
    container_name: str
    create_args: tuple[str, ...]


async def _control(
    workload: DockerStdioWorkload,
    args: tuple[str, ...],
    environment: dict[str, str],
    operation: str,
) -> None:
    """Bound Docker control and redact private argv and daemon diagnostics."""
    process = await asyncio.create_subprocess_exec(
        workload.docker_binary,
        *args,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=10)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError(f"MCP Docker {operation} failed for {workload.container_name}")


@asynccontextmanager
async def owned_docker_stdio(workload: DockerStdioWorkload, params: Any, stdio_client: Any):
    """Create before attach and remove after SDK shutdown on every owned exit."""
    from mcp.client.stdio import get_default_environment

    environment = {**get_default_environment(), **(params.env or {})}
    created = False
    try:
        await _control(workload, workload.create_args, environment, "creation")
        created = True
        attached = params.model_copy(
            update={
                "command": workload.docker_binary,
                "args": ["start", "--attach", "--interactive", workload.container_name],
            }
        )
        async with stdio_client(attached) as transport:
            yield transport
    finally:
        if created:
            await cleanup_despite_cancellation(
                _control(
                    workload, ("rm", "--force", workload.container_name), environment, "cleanup"
                )
            )
