"""Native tool handlers and tool-schema helpers for the live scenario."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from live_scenarios.research_audit.contracts import (
    FetchUrlInput,
    FetchUrlOutput,
    ReadFileExcerptInput,
    ReadFileExcerptOutput,
    WebSearchInput,
    WebSearchOutput,
)


def build_tool_schema(
    *,
    name: str,
    description: str,
    input_model: type,
) -> dict[str, Any]:
    """Build an OpenAI-compatible function tool schema from a Pydantic model."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": input_model.model_json_schema(),
        },
    }


def build_read_file_excerpt_handler(repo_root: Path):
    """Return a bounded local file-read handler rooted at the repository."""

    repo_root = repo_root.resolve()

    async def handler(_ctx: Any, data: ReadFileExcerptInput) -> dict[str, Any]:
        target = Path(data.path).expanduser().resolve()
        if repo_root not in target.parents and target != repo_root:
            raise ValueError(f"path {target} is outside repo root {repo_root}")
        lines = target.read_text(encoding="utf-8").splitlines()
        start = data.start_line
        end = max(start, data.end_line)
        excerpt = "\n".join(lines[start - 1 : end])
        return ReadFileExcerptOutput(
            path=str(target),
            start_line=start,
            end_line=end,
            content=excerpt,
        ).model_dump(mode="json")

    return handler


async def web_search_handler(_ctx: Any, data: WebSearchInput) -> dict[str, Any]:
    """Search the web using an env-driven provider, with an offline fallback."""

    provider = os.getenv("LIVE_SCENARIO_SEARCH_PROVIDER", "").strip().lower() or "offline"
    api_key = os.getenv("LIVE_SCENARIO_SEARCH_API_KEY", "")
    if provider == "tavily" and api_key:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": data.query,
                    "max_results": data.max_results,
                    "search_depth": "basic",
                },
            )
            response.raise_for_status()
            payload = response.json()
        items = [
            {
                "title": item.get("title", item.get("url", "")),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in payload.get("results", [])
        ]
        return WebSearchOutput(query=data.query, provider="tavily", items=items).model_dump(
            mode="json"
        )

    if provider == "serper" and api_key:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key},
                json={"q": data.query, "num": data.max_results},
            )
            response.raise_for_status()
            payload = response.json()
        items = [
            {
                "title": item.get("title", item.get("link", "")),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
            for item in payload.get("organic", [])
        ]
        return WebSearchOutput(query=data.query, provider="serper", items=items).model_dump(
            mode="json"
        )

    return WebSearchOutput(
        query=data.query,
        provider="offline",
        items=[],
    ).model_dump(mode="json")


async def fetch_url_handler(_ctx: Any, data: FetchUrlInput) -> dict[str, Any]:
    """Fetch and lightly summarize a remote URL."""

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(data.url)
        response.raise_for_status()
    text = response.text[: data.max_chars]
    title = None
    lowered = text.lower()
    if "<title>" in lowered and "</title>" in lowered:
        start = lowered.index("<title>") + len("<title>")
        end = lowered.index("</title>", start)
        title = text[start:end].strip()
    return FetchUrlOutput(
        url=str(response.url),
        status_code=response.status_code,
        title=title,
        content=text,
    ).model_dump(mode="json")
