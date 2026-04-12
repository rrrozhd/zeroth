"""Production-shaped entrypoint that extends ``zeroth.core.service.entrypoint``.

The stock ``python -m zeroth.core.service.entrypoint`` command loads a
deployment by ref and boots FastAPI, but it does **not** register any
agent runners — that's user code. This module is the canonical
"drop-in extension" pattern: import the stock bootstrap pieces, build
your :class:`AgentRunner` instances, wire them into the orchestrator,
and hand the app back to uvicorn.

Run
---
    # One-shot seed (first time only)
    ZEROTH_DATABASE__SQLITE_PATH=examples_service.sqlite \\
        uv run python examples/service/seed_deployment.py

    # Start the service
    ZEROTH_DATABASE__SQLITE_PATH=examples_service.sqlite \\
        ZEROTH_DEPLOYMENT_REF=examples-api \\
        ZEROTH_REGULUS__ENABLED=false \\
        ZEROTH_WEBHOOK__ENABLED=false \\
        ZEROTH_APPROVAL_SLA__ENABLED=false \\
        ZEROTH_REDIS__MODE=disabled \\
        OPENAI_API_KEY=sk-... \\
        uv run python examples/service/entrypoint.py

Stop with ``Ctrl-C``.

The env-var shape exactly matches what the stock entrypoint reads in
``zeroth.core.service.entrypoint`` — this file only adds the
``agent_runners`` and auth-config bits the stock version leaves blank.
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import asyncio
import os
import sys

import uvicorn

from examples._contracts import Answer, Question
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
)
from zeroth.core.config.settings import get_settings
from zeroth.core.identity import ServiceRole
from zeroth.core.service.app import create_app
from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.core.service.bootstrap import bootstrap_service
from zeroth.core.storage.factory import create_database


async def build_app_async():
    """Mirror of ``zeroth.core.service.entrypoint._bootstrap`` with agent runners."""
    settings = get_settings()
    database = await create_database(settings)

    deployment_ref = os.environ.get("ZEROTH_DEPLOYMENT_REF", "examples-api")

    runner = AgentRunner(
        AgentConfig(
            name="qa",
            description="Answers questions via LiteLLM.",
            instruction="Answer briefly.",
            model_name="openai/gpt-4o-mini",
            input_model=Question,
            output_model=Answer,
        ),
        LiteLLMProviderAdapter(),
    )

    auth_config = ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="examples-demo",
                secret=os.environ.get("ZEROTH_EXAMPLE_API_KEY", "demo-operator-key"),
                subject="examples-demo",
                roles=[ServiceRole.OPERATOR, ServiceRole.REVIEWER],
                tenant_id="default",
                workspace_id=None,
            )
        ]
    )

    bootstrap = await bootstrap_service(
        database,
        deployment_ref=deployment_ref,
        agent_runners={"qa": runner},
        auth_config=auth_config,
        enable_durable_worker=True,
    )
    return create_app(bootstrap)


def app_factory():
    """Uvicorn factory — async-to-sync bridge, same pattern as the stock entrypoint."""
    return asyncio.run(build_app_async())


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run(
        "examples.service.entrypoint:app_factory",
        host=host,
        port=port,
        factory=True,
        proxy_headers=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
