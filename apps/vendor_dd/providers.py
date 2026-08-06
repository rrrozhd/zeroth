"""Provider selection for the vendor-dd agents.

Real mode (``VENDOR_DD_REAL_LLM=1`` + a provider key): the stock
``LiteLLMProviderAdapter`` — every agent node's ``model_provider`` is honored.

Hermetic mode (default): ``ScriptedProviderAdapter`` — a deterministic
implementation of the ``ProviderAdapter`` protocol that routes on the
instruction tag each agent carries, parses the real input payload out of the
assembled prompt, and produces schema-conforming outputs (including a genuine
tool-call round for the screening agent). The whole service — orchestration,
sandboxes, fan-out, approvals, audits — runs exactly as in production; only
the model is scripted.
"""

from __future__ import annotations

import json
import os
from typing import Any

from apps.vendor_dd.graphs import CHAT_TAG, DIMENSION_TAG, REPORT_TAG, SCREEN_TAG
from zeroth.runtime.agents import (
    LiteLLMProviderAdapter,
    ProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)

_DECODER = json.JSONDecoder()


_ROLE_ALIASES = {"ai": "assistant", "human": "user"}


def _message_parts(message: Any) -> tuple[str, str]:
    """Return (role, content) for PromptMessage objects, dicts, and LangChain.

    messages (which carry ``type`` — e.g. ToolMessage(type="tool") — instead
    of ``role``).
    """
    if isinstance(message, dict):
        role = message.get("role") or message.get("type") or ""
        content = message.get("content", "")
    else:
        role = getattr(message, "role", None) or getattr(message, "type", "") or ""
        content = getattr(message, "content", "")
    role = str(role).lower()
    return _ROLE_ALIASES.get(role, role), str(content)


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object found in a text block."""
    start = text.find("{")
    while start != -1:
        try:
            value, _ = _DECODER.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    return None


class ScriptedProviderAdapter:
    """Deterministic stand-in model for hermetic runs."""

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        system = ""
        payload: dict[str, Any] = {}
        tool_verdicts: list[dict[str, Any]] = []
        user_turns: list[str] = []
        for message in request.messages:
            role, content = _message_parts(message)
            if role == "system" and not system:
                system = content
            elif role == "user":
                if "Input payload:" in content and not payload:
                    parsed = _first_json_object(content)
                    if parsed:
                        payload = parsed
                else:
                    user_turns.append(content)
            elif role == "tool":
                parsed = _first_json_object(content)
                if parsed and "listed" in parsed:
                    tool_verdicts.append(parsed)
            # Assistant tool-call echoes are ignored.

        if SCREEN_TAG in system:
            return self._screen(payload, tool_verdicts)
        if DIMENSION_TAG in system:
            return self._dimension(payload)
        if REPORT_TAG in system:
            return self._report(payload)
        if CHAT_TAG in system:
            return self._chat(payload, request)
        return ProviderResponse(
            content={"error": "scripted provider: unrecognized agent"}, raw=None
        )

    # -- screening analyst --------------------------------------------------

    def _screen(self, payload: dict[str, Any], verdicts: list[dict[str, Any]]) -> ProviderResponse:
        names = [payload.get("vendor_name", "")]
        names.extend(list(payload.get("subprocessors") or [])[:2])
        names = [name for name in names if name]
        if not verdicts:
            calls = [
                {"id": f"call-{i}", "name": "sanctions_screen", "args": {"name": name}}
                for i, name in enumerate(names, start=1)
            ]
            return ProviderResponse(content=None, raw=None, tool_calls=calls)

        hits = [v for v in verdicts if v.get("listed")]
        flags: list[str] = []
        if payload.get("data_access") == "regulated_pii":
            flags.append("processes regulated personal data (TPR-001 Tier A)")
        if payload.get("spend_band") in ("medium", "large"):
            flags.append("spend above financial-review threshold (TPR-003)")
        for hit in hits:
            flags.append(f"sanctions match: {hit.get('name')} on {hit.get('list_name')} (TPR-002)")
        citations = [
            "policy/tpr-001-data-classification",
            "policy/tpr-002-sanctions",
        ]
        if payload.get("spend_band") in ("medium", "large"):
            citations.append("policy/tpr-003-financial-viability")
        report = {
            "vendor_slug": payload.get("vendor_slug", ""),
            "vendor_name": payload.get("vendor_name", ""),
            "jurisdiction": payload.get("jurisdiction", ""),
            "category": payload.get("category", ""),
            "annual_spend_usd": payload.get("annual_spend_usd", 0.0),
            "data_access": payload.get("data_access", "internal"),
            "data_risk": payload.get("data_risk", 1),
            "spend_band": payload.get("spend_band", "small"),
            "dimensions": payload.get("dimensions", []),
            "summary": (
                f"Screened {payload.get('vendor_name')} and "
                f"{max(len(names) - 1, 0)} subprocessor(s): "
                f"{len(hits)} sanctions hit(s); {len(flags)} flag(s) raised."
            ),
            "flags": flags,
            "sanctions_status": "hit" if hits else "clear",
            "policy_citations": citations,
        }
        return ProviderResponse(content=report, raw=report)

    # -- dimension analyst ---------------------------------------------------

    def _dimension(self, payload: dict[str, Any]) -> ProviderResponse:
        dimension = payload.get("dimension", "general")
        metrics = payload.get("financial_metrics") or {}
        flags = list(payload.get("flags") or [])
        red_flags: list[str] = []
        rating = 2
        if dimension == "financial":
            if metrics.get("going_concern_flag"):
                rating = 5
                red_flags.append("going-concern flag: current ratio below 1.0")
            elif metrics.get("avg_net_margin_pct", 0) < 5:
                rating = 3
            if metrics.get("revenue_trend_pct", 0) < 0:
                red_flags.append("declining revenue trend")
                rating = max(rating, 4)
        elif dimension == "security":
            if payload.get("data_access") == "regulated_pii":
                rating = 4
                red_flags.append("regulated PII crosses the vendor boundary")
            if any("sanctions" in flag for flag in flags):
                red_flags.append("subprocessor exposure includes a listed entity")
                rating = max(rating, 4)
        elif dimension == "compliance":
            if payload.get("sanctions_status") == "hit":
                rating = 5
                red_flags.append("sanctions denylist match — onboarding blocked by TPR-002")
            elif payload.get("data_access") == "regulated_pii":
                rating = 3
        finding = {
            "dimension": dimension,
            "vendor_slug": payload.get("vendor_slug", ""),
            "rating": rating,
            "rationale": (
                f"{dimension.capitalize()} review of {payload.get('vendor_name')}: "
                f"rated {rating}/5 based on the brief, flags, and metrics provided."
            ),
            "red_flags": red_flags,
        }
        return ProviderResponse(content=finding, raw=finding)

    # -- report writer ---------------------------------------------------------

    def _report(self, payload: dict[str, Any]) -> ProviderResponse:
        tier = payload.get("tier", "medium")
        decision = {
            "low": "approved",
            "medium": "conditional",
            "high": "conditional",
            "critical": "rejected",
        }.get(tier, "conditional")
        summaries = [
            {
                "dimension": finding.get("dimension"),
                "rating": finding.get("rating"),
                "note": finding.get("rationale", ""),
            }
            for finding in payload.get("panel") or []
            if isinstance(finding, dict)
        ]
        conditions: list[str] = []
        if tier != "low":
            conditions.append("annual reassessment per TPR-001")
        if payload.get("sanctions_status") == "hit":
            conditions.append("subprocessor replacement required before renewal (TPR-002)")
        if any(
            "going-concern" in flag
            for finding in payload.get("panel") or []
            if isinstance(finding, dict)
            for flag in finding.get("red_flags", [])
        ):
            conditions.append("quarterly financial monitoring (TPR-003)")
        report = {
            "vendor_name": payload.get("vendor_name", ""),
            "vendor_slug": payload.get("vendor_slug", ""),
            "tier": tier,
            "risk_score": payload.get("risk_score", 0.0),
            "decision": decision,
            "executive_summary": (
                f"{payload.get('vendor_name')} assessed at tier '{tier}' "
                f"(score {payload.get('risk_score')}). Drivers: "
                f"{'; '.join(payload.get('drivers') or []) or 'none recorded'}."
            ),
            "dimension_summaries": summaries,
            "conditions": conditions,
        }
        return ProviderResponse(content=report, raw=report)

    # -- follow-up chat ---------------------------------------------------------

    def _chat(self, payload: dict[str, Any], request: ProviderRequest) -> ProviderResponse:
        # Conversation turns (stored + incoming) are rendered as chat messages
        # after the first user message; count the prior human turns to prove
        # cross-run persistence.
        turns: list[str] = []
        seen_payload_message = False
        for message in request.messages:
            role, content = _message_parts(message)
            if role == "user":
                if not seen_payload_message and "Input payload:" in content:
                    seen_payload_message = True
                    continue
                turns.append(content)
        current = turns[-1] if turns else ""
        earlier = turns[:-1]
        if earlier:
            reply = (
                f"This thread has {len(earlier)} earlier turn(s); the first was: "
                f"{earlier[0]!r}. On your current question ({current!r}): the "
                "assessment stands as recorded — tiers and drivers are in the "
                "run's audit trail."
            )
        else:
            reply = (
                f"Noted your question ({current!r}). This is the first turn on "
                "this thread; ask a follow-up on the same thread_id and I will "
                "remember this exchange."
            )
        answer = {"reply": reply}
        return ProviderResponse(content=answer, raw=answer)


def select_provider() -> ProviderAdapter:
    """Pick the provider adapter for this process."""
    if os.environ.get("VENDOR_DD_REAL_LLM") == "1":
        return LiteLLMProviderAdapter()
    return ScriptedProviderAdapter()


def is_hermetic() -> bool:
    """True when the scripted provider is in use."""
    return os.environ.get("VENDOR_DD_REAL_LLM") != "1"
