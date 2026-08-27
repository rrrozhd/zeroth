"""Post-Check publication of the complete campaign evidence bundle."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, replace

from .criteria import original_acceptance_criteria
from .evidence import AcceptanceCriterion, EvidenceStore
from .ledger import CampaignLedger

_DERIVED_IDS = (
    "evidence.acceptance",
    "evidence.report",
    "evidence.sha256-checksums",
)


def _escape_markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


class EvidenceFirstCampaignFinalizer:
    """Generate final records only after the complete original catalog passes."""

    def finalize(self, *, store: EvidenceStore, ledger: CampaignLedger) -> None:
        if ledger.store.root != store.root:
            raise ValueError("acceptance ledger belongs to a different evidence bundle")
        catalog = original_acceptance_criteria()
        expected_ids = tuple(item.criterion_id for item in catalog)
        actual = ledger.criteria
        if tuple(item.criterion_id for item in actual) != expected_ids:
            raise ValueError("final acceptance catalog must match the complete original catalog")

        by_id = {item.criterion_id: item for item in actual}
        prerequisites = tuple(
            item for item in actual if item.criterion_id not in _DERIVED_IDS
        )
        incomplete = tuple(item.criterion_id for item in prerequisites if item.status != "pass")
        if incomplete:
            raise RuntimeError(
                "campaign criteria are not all passing: " + ", ".join(incomplete)
            )
        store.validate_evidence_references(prerequisites)
        store.scan_recursive()

        derived_states = tuple(by_id[item] for item in _DERIVED_IDS)
        invalid_derived = tuple(
            item.criterion_id
            for item in derived_states
            if item.status not in {"not_run", "pass"}
        )
        if invalid_derived:
            raise RuntimeError(
                "derived finalization criteria are not admissible: "
                + ", ".join(invalid_derived)
            )

        if all(item.status == "not_run" for item in derived_states):
            event_id = str(uuid.uuid4())
            reference = f"events.ndjson#{event_id}"
            predicted = tuple(
                replace(item, status="pass", evidence=(reference,))
                if item.criterion_id in _DERIVED_IDS
                else item
                for item in actual
            )
            report = self._render_report(store, predicted)
            store.validate(report)
            store.validate([asdict(item) for item in predicted])
            store.append_event(
                "campaign.finalization.ready",
                {
                    "catalog_size": len(predicted),
                    "derived_criteria": list(_DERIVED_IDS),
                    "handoff_references": self._handoff_references(predicted),
                },
                event_id=event_id,
            )
            for criterion_id in _DERIVED_IDS:
                ledger.record(criterion_id, "pass", evidence=(reference,))
        elif all(item.status == "pass" for item in derived_states):
            predicted = ledger.criteria
            report = self._render_report(store, predicted)
            store.validate(report)
            store.validate([asdict(item) for item in predicted])
        else:
            raise RuntimeError("derived finalization criteria are only partially recorded")

        acceptance = ledger.criteria
        if tuple(item.criterion_id for item in acceptance) != expected_ids or any(
            item.status != "pass" for item in acceptance
        ):
            raise RuntimeError("full original acceptance catalog is not all passing")
        report = self._render_report(store, acceptance)
        store.validate(report)
        store.validate([asdict(item) for item in acceptance])
        store.validate_evidence_references(acceptance)
        store.finalize_bundle(acceptance=acceptance, report_markdown=report)
        ledger.mark_finalized()

    @staticmethod
    def _handoff_references(
        acceptance: tuple[AcceptanceCriterion, ...],
    ) -> list[str]:
        return sorted(
            {
                reference
                for item in acceptance
                if item.criterion_id.startswith("handoff.")
                for reference in item.evidence
            }
        )

    def _render_report(
        self,
        store: EvidenceStore,
        acceptance: tuple[AcceptanceCriterion, ...],
    ) -> str:
        events = store.read_events()
        event_counts = Counter(str(event.get("type", "unknown")) for event in events)
        manifest_path = store.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        revision = (
            manifest.get("revision", "unavailable")
            if isinstance(manifest, dict)
            else "unavailable"
        )
        if not isinstance(revision, str):
            revision = "unavailable"
        correlations = {
            str(field): str(value)
            for event in events
            if isinstance(event.get("correlation"), dict)
            for field, value in event["correlation"].items()
        }
        lines = [
            "# Full Evidence-First Live Evaluation",
            "",
            "Status: **PASS**",
            "",
            f"Revision: `{_escape_markdown(revision)}`",
            "",
            f"Acceptance criteria: {len(acceptance)} passed, 0 failed, 0 blocked.",
            "",
            "## Event inventory",
            "",
            "| Event type | Count |",
            "| --- | ---: |",
        ]
        lines.extend(
            f"| `{_escape_markdown(event_type)}` | {count} |"
            for event_type, count in sorted(event_counts.items())
        )
        lines.extend(
            [
                "",
                f"Typed correlation identities observed: {len(correlations)}.",
                "",
                "## Acceptance ledger",
                "",
                "| Criterion | Status | Evidence |",
                "| --- | --- | --- |",
            ]
        )
        for item in acceptance:
            evidence = ", ".join(f"`{_escape_markdown(ref)}`" for ref in item.evidence)
            lines.append(
                f"| `{_escape_markdown(item.criterion_id)}` | {item.status} | {evidence} |"
            )
        lines.extend(["", "## Handoff evidence", ""])
        handoff = tuple(item for item in acceptance if item.criterion_id.startswith("handoff."))
        for item in handoff:
            references = ", ".join(
                f"`{_escape_markdown(reference)}`" for reference in item.evidence
            )
            lines.append(f"- `{item.criterion_id}`: {references}")
        lines.extend(
            [
                "",
                "The checksum manifest seals this report, the full acceptance ledger, "
                "and every preceding artifact. Provider-project usage remains an "
                "upper-bound cross-check; tagged campaign identities are authoritative.",
                "",
            ]
        )
        return "\n".join(lines)
