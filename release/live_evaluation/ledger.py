"""Fail-closed acceptance state machine for a live evaluation campaign."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from .evidence import AcceptanceCriterion, EvidenceStore

RecordedStatus = Literal["pass", "fail", "blocked"]


class CampaignHaltedError(RuntimeError):
    """A stop condition failed and no further campaign work is admissible."""


class CampaignLedger:
    def __init__(
        self, store: EvidenceStore, criteria: Sequence[AcceptanceCriterion]
    ) -> None:
        self.store = store
        self._criteria = {criterion.criterion_id: criterion for criterion in criteria}
        if len(self._criteria) != len(criteria):
            raise ValueError("acceptance criterion identifiers must be unique")
        self._halt_reason: str | None = None
        self._finalized = False
        self._resume_from_events()

    def _resume_from_events(self) -> None:
        for event in self.store.read_events():
            event_type = event.get("type")
            data = event.get("data")
            if event_type != "acceptance.recorded":
                continue
            if not isinstance(data, dict):
                raise ValueError("malformed durable acceptance event")
            criterion_id = data.get("criterion_id")
            status = data.get("status")
            evidence = data.get("evidence", [])
            note = data.get("note")
            if (
                not isinstance(criterion_id, str)
                or status not in {"pass", "fail", "blocked"}
                or not isinstance(evidence, list)
                or not all(isinstance(item, str) for item in evidence)
                or (note is not None and not isinstance(note, str))
            ):
                raise ValueError("malformed durable acceptance event")
            try:
                current = self._criteria[criterion_id]
            except KeyError as exc:
                raise ValueError(
                    f"durable event references unknown criterion: {criterion_id}"
                ) from exc
            if current.status != "not_run":
                raise ValueError(f"duplicate durable acceptance event: {criterion_id}")
            self._criteria[criterion_id] = replace(
                current,
                status=status,
                evidence=tuple(evidence),
                note=note,
            )
            if criterion_id.startswith("stop.") and status == "fail":
                self._halt_reason = criterion_id

    @property
    def criteria(self) -> tuple[AcceptanceCriterion, ...]:
        return tuple(self._criteria.values())

    @property
    def halted(self) -> bool:
        return self._halt_reason is not None

    @property
    def may_run_check(self) -> bool:
        workflow = [
            criterion
            for criterion in self._criteria.values()
            if criterion.criterion_id.startswith(("workflow1.", "workflow2.", "workflow3."))
        ]
        return not self.halted and bool(workflow) and all(
            criterion.status == "pass" for criterion in workflow
        )

    def record(
        self,
        criterion_id: str,
        status: RecordedStatus,
        *,
        evidence: tuple[str, ...] = (),
        note: str | None = None,
    ) -> None:
        if self._finalized:
            raise RuntimeError("acceptance ledger is finalized")
        if self.halted:
            raise CampaignHaltedError(f"campaign halted by {self._halt_reason}")
        try:
            current = self._criteria[criterion_id]
        except KeyError as exc:
            raise KeyError(f"unknown acceptance criterion: {criterion_id}") from exc
        if current.status != "not_run":
            raise ValueError(f"criterion {criterion_id!r} already recorded")
        if status in {"pass", "fail"} and not evidence:
            raise ValueError("pass/fail requires at least one durable evidence reference")
        self.store.append_event(
            "acceptance.recorded",
            {
                "criterion_id": criterion_id,
                "evidence": list(evidence),
                "note": note,
                "status": status,
            },
        )
        self._criteria[criterion_id] = replace(
            current, status=status, evidence=evidence, note=note
        )
        if criterion_id.startswith("stop.") and status == "fail":
            self._halt_reason = criterion_id
            self.store.append_event("campaign.halted", {"criterion_id": criterion_id})

    def resolved_criteria(self) -> tuple[AcceptanceCriterion, ...]:
        """Return the final fail-closed view without writing acceptance.json."""
        if self._finalized:
            raise RuntimeError("acceptance ledger is finalized")
        resolved = dict(self._criteria)
        if self.halted:
            for criterion_id, criterion in tuple(resolved.items()):
                if criterion.status == "not_run":
                    resolved[criterion_id] = replace(
                        criterion,
                        status="blocked",
                        note=f"campaign halted by {self._halt_reason}",
                    )
        return tuple(resolved.values())

    def mark_finalized(self) -> None:
        self._finalized = True
