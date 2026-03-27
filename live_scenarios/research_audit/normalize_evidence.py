"""Wrapped-command executable unit for normalizing audit evidence."""

from __future__ import annotations

import json
import sys

from live_scenarios.research_audit.contracts import AuditState, EvidenceItem


def main() -> int:
    payload = json.load(sys.stdin)
    state = AuditState.model_validate(payload)

    deduped_evidence: list[EvidenceItem] = []
    seen_evidence: set[tuple[str, str, str | None, str | None, str | None]] = set()
    for item in state.evidence:
        key = (item.kind, item.title, item.location, item.url, item.snippet)
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        deduped_evidence.append(item)

    deduped_sources: list[str] = []
    seen_sources: set[str] = set()
    for source in list(state.sources) + [
        item.location or item.url for item in deduped_evidence if item.location or item.url
    ]:
        if source is None or source in seen_sources:
            continue
        seen_sources.add(source)
        deduped_sources.append(source)

    normalized = state.model_copy(
        update={
            "evidence": deduped_evidence,
            "sources": deduped_sources,
            "summary": state.summary
            or f"Normalized {len(deduped_evidence)} evidence item(s) for review.",
        }
    )
    print(normalized.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
