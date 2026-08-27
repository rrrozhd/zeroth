"""Fail-closed ingestion and evaluation of campaign-wide evidence.

This module does not launch browsers, resolve secrets, call providers, or seal an
evidence bundle.  It consumes the outputs of those independently controlled
activities and refuses to infer success from a missing result.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .coordinator import ActionRecorder, CriterionResult, Phase, StepResult
from .criteria import original_acceptance_criteria
from .evidence import EvidenceStore, UnsafeEvidenceError
from .reconciliation import ReconciliationResult

_FINALIZATION_CRITERIA = frozenset(
    {
        "evidence.acceptance",
        "evidence.report",
        "evidence.sha256-checksums",
    }
)
_ARTIFACT_EVIDENCE = {
    "evidence.screenshots": "screenshots",
    "evidence.playwright-html-report": "playwright-report",
    "evidence.videos": "videos",
    "evidence.accessibility-results": "accessibility",
}
_HANDOFF_DESTINATIONS = {
    "handoff.discrepancy-register": "handoff/discrepancies.md",
    "handoff.execution-and-rollback-instructions": "handoff/execution-and-rollback.md",
    "handoff.project-model-updated": "handoff/project-model.md",
}
_DERIVED_STOP_SOURCES: Mapping[str, tuple[str, ...]] = {
    "stop.no-secret-artifact": (
        "audit.zero-secrets",
        "evidence.secret-rejection",
    ),
    "stop.cost-cap-enforced": ("economics.campaign-and-run-caps",),
    "stop.health-matches-graph": (
        "workflow1.health-exact-graph-version",
        "workflow2.health-exact-graph-version",
        "workflow3.health-exact-graph-version",
    ),
    "stop.audit-complete-and-valid": tuple(
        item.criterion_id
        for item in original_acceptance_criteria()
        if item.criterion_id.startswith("audit.")
    ),
    "stop.rejection-zero-effects": ("workflow3.negative-rejection-zero-marker",),
    "stop.no-ambiguous-auto-retry": ("workflow3.ambiguous-no-reexecution",),
    "stop.no-economic-double-count": (
        "economics.one-event-per-noncache-call",
        "economics.reconciled-totals",
    ),
}


@dataclass(frozen=True, slots=True)
class CheckCommandResult:
    """Sanitized result produced by an actual Zeroth Check invocation."""

    argv: tuple[str, ...]
    working_directory: Path
    exit_code: int
    stdout: str
    stderr: str
    verdict: Literal["pass", "fail"]


@dataclass(frozen=True, slots=True)
class PlaywrightProductionResult:
    """Bounded browser command result plus its external artifact root."""

    artifact_root: Path
    argv: tuple[str, ...]
    working_directory: Path
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class CrossCuttingSources:
    """Explicit, already-produced inputs; absence always means blocked."""

    playwright_root: Path | None = None
    reconciliation: ReconciliationResult | None = None
    playwright_producer: Callable[[], PlaywrightProductionResult] | None = None
    reconciliation_collector: Callable[[EvidenceStore], ReconciliationResult] | None = None
    handoff_documents: Mapping[str, Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.playwright_root is not None and self.playwright_producer is not None:
            raise ValueError("configure a Playwright root or producer, not both")
        if self.reconciliation is not None and self.reconciliation_collector is not None:
            raise ValueError("configure a reconciliation result or collector, not both")


@dataclass(frozen=True, slots=True)
class _Assertion:
    status: Literal["pass", "fail"]
    test_id: str
    evidence: tuple[str, ...]


class EvidenceFirstCrossCuttingGateExecutor:
    """Implement the campaign entrypoint's cross-cutting executor seam."""

    def __init__(
        self,
        store: EvidenceStore,
        sources: CrossCuttingSources,
        *,
        check_runner: Callable[[], CheckCommandResult] | None = None,
    ) -> None:
        self.store = store
        self.sources = sources
        self.check_runner = check_runner
        self._assertions: dict[str, _Assertion] | None = None
        self._artifact_references: tuple[str, ...] = ()
        self._playwright_root = sources.playwright_root
        self._reconciliation = sources.reconciliation
        self._reconciliation_collected = sources.reconciliation is not None

    def execute(
        self,
        *,
        phase: Phase,
        criterion_ids: tuple[str, ...],
        recorder: ActionRecorder,
    ) -> StepResult:
        if recorder.store.root != self.store.root:
            raise ValueError("cross-cutting recorder belongs to a different evidence bundle")
        if phase is Phase.CHECK:
            return self._execute_check(criterion_ids, recorder)
        if phase is not Phase.CROSS_CUTTING:
            raise ValueError("cross-cutting executor received an unsupported phase")

        try:
            assertions = self._load_playwright_artifacts(recorder)
            self._collect_reconciliation()
            handoff = self._ingest_handoff_documents()
            results = tuple(
                self._evaluate_cross_cutting(criterion_id, assertions, handoff)
                for criterion_id in criterion_ids
            )
        except (OSError, ValueError, UnsafeEvidenceError) as exc:
            reason = getattr(exc, "code", "evidence_policy_rejection")
            event_id = self.store.append_event(
                "campaign.cross_cutting.evidence_rejected",
                {"exception_type": type(exc).__name__, "reason": reason},
            )
            evidence = f"events.ndjson#{event_id}"
            results = tuple(
                CriterionResult(
                    criterion_id,
                    "fail" if criterion_id.startswith("stop.") else "blocked",
                    (evidence,) if criterion_id.startswith("stop.") else (),
                    f"cross-cutting input blocked: {reason}",
                )
                for criterion_id in criterion_ids
            )
        return StepResult(results)

    def _load_playwright_artifacts(
        self, recorder: ActionRecorder
    ) -> Mapping[str, _Assertion]:
        if self._assertions is not None:
            return self._assertions
        root = self._playwright_root
        if root is None and self.sources.playwright_producer is not None:
            produced = self.sources.playwright_producer()
            recorder.record_command_result(
                name="playwright-campaign",
                argv=produced.argv,
                working_directory=produced.working_directory,
                exit_code=produced.exit_code,
                stdout=produced.stdout,
                stderr=produced.stderr,
            )
            if produced.exit_code != 0:
                raise RuntimeError("Playwright campaign command did not pass")
            root = produced.artifact_root
            self._playwright_root = root
        if root is None:
            self._assertions = {}
            return self._assertions
        root = root.resolve(strict=True)
        result_path = root / "results.json"
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.store.validate(payload)
        if payload.get("schema_version") != 1 or payload.get("completed") is not True:
            raise ValueError("Playwright result is incomplete or uses an unsupported schema")
        artifacts = payload.get("artifacts")
        rows = payload.get("criteria")
        if not isinstance(artifacts, list) or not isinstance(rows, list):
            raise ValueError("Playwright result must declare artifacts and criteria")

        ingested: list[str] = []
        declared: set[str] = set()
        for row in artifacts:
            if not isinstance(row, dict):
                raise ValueError("invalid Playwright artifact declaration")
            source_name = row.get("source")
            destination = row.get("destination")
            if not isinstance(source_name, str) or not isinstance(destination, str):
                raise ValueError("invalid Playwright artifact path")
            source = (root / source_name).resolve(strict=True)
            source.relative_to(root)
            target = self.store.ingest_artifact(source, destination)
            reference = target.relative_to(self.store.root).as_posix()
            if reference in declared:
                raise ValueError("duplicate Playwright artifact destination")
            declared.add(reference)
            ingested.append(reference)
        result_target = self.store.ingest_artifact(
            result_path, "playwright-report/results.json"
        )
        ingested.append(result_target.relative_to(self.store.root).as_posix())

        assertions: dict[str, _Assertion] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid Playwright criterion result")
            criterion_id = row.get("criterion_id")
            status = row.get("status")
            test_id = row.get("test_id")
            evidence = row.get("evidence")
            if (
                not isinstance(criterion_id, str)
                or status not in {"pass", "fail"}
                or not isinstance(test_id, str)
                or not test_id.strip()
                or not isinstance(evidence, list)
                or not evidence
                or not all(isinstance(item, str) and item in declared for item in evidence)
            ):
                raise ValueError("criterion assertion lacks actual declared artifacts")
            if criterion_id in assertions:
                raise ValueError("duplicate Playwright criterion assertion")
            assertions[criterion_id] = _Assertion(status, test_id, tuple(evidence))
        self._assertions = assertions
        self._artifact_references = tuple(ingested)
        return assertions

    def _collect_reconciliation(self) -> None:
        if self._reconciliation_collected:
            return
        collector = self.sources.reconciliation_collector
        if collector is not None:
            self._reconciliation = collector(self.store)
        self._reconciliation_collected = True

    def _ingest_handoff_documents(self) -> Mapping[str, str]:
        references: dict[str, str] = {}
        for criterion_id, destination in _HANDOFF_DESTINATIONS.items():
            source = self.sources.handoff_documents.get(criterion_id)
            if source is None:
                continue
            content = source.read_text(encoding="utf-8").strip()
            if not content:
                raise ValueError("handoff document must not be empty")
            lowered = content.lower()
            required_terms = {
                "handoff.discrepancy-register": ("discrep", "reconcil"),
                "handoff.execution-and-rollback-instructions": ("execution", "rollback"),
                "handoff.project-model-updated": ("project model", "runtime", "risk"),
            }[criterion_id]
            if any(term not in lowered for term in required_terms):
                raise ValueError("handoff document is missing required campaign sections")
            target = self.store.ingest_artifact(source, destination)
            references[criterion_id] = target.relative_to(self.store.root).as_posix()
        return references

    def _evaluate_cross_cutting(
        self,
        criterion_id: str,
        assertions: Mapping[str, _Assertion],
        handoff: Mapping[str, str],
    ) -> CriterionResult:
        if criterion_id in _FINALIZATION_CRITERIA:
            return CriterionResult(
                criterion_id,
                "blocked",
                (),
                "owned by post-Check finalization; prefinal executor does not seal",
            )
        if criterion_id == "evidence.manifest":
            return self._file_result(criterion_id, "manifest.json")
        if criterion_id == "evidence.events":
            return self._file_result(criterion_id, "events.ndjson")
        if criterion_id == "evidence.command-output-and-exit-codes":
            commands = tuple(sorted((self.store.root / "commands").glob("*.json")))
            if not commands:
                return self._blocked(criterion_id, "no persisted command result exists")
            return CriterionResult(
                criterion_id,
                "pass",
                tuple(path.relative_to(self.store.root).as_posix() for path in commands),
            )
        category = _ARTIFACT_EVIDENCE.get(criterion_id)
        if category is not None:
            matches = tuple(
                ref
                for ref in self._artifact_references
                if ref.startswith(category + "/")
            )
            if not matches:
                return self._blocked(criterion_id, f"no {category} artifact was ingested")
            return CriterionResult(criterion_id, "pass", matches)
        if criterion_id in handoff:
            if self._reconciliation is None:
                return self._blocked(
                    criterion_id, "handoff cannot precede campaign reconciliation"
                )
            return CriterionResult(criterion_id, "pass", (handoff[criterion_id],))

        reconciled = {
            result.criterion_id: result
            for result in (
                self._reconciliation.criteria
                if self._reconciliation is not None
                else ()
            )
        }
        if criterion_id in reconciled:
            return reconciled[criterion_id]
        if criterion_id in _DERIVED_STOP_SOURCES:
            return self._derive_stop(criterion_id, assertions, reconciled)
        assertion = assertions.get(criterion_id)
        if assertion is not None:
            event_id = self.store.append_event(
                "campaign.cross_cutting.assertion_consumed",
                {
                    "criterion_id": criterion_id,
                    "status": assertion.status,
                    "test_id": assertion.test_id,
                },
            )
            return CriterionResult(
                criterion_id,
                assertion.status,
                (*assertion.evidence, f"events.ndjson#{event_id}"),
            )
        return self._blocked(criterion_id, "no actual artifact or reconciled result supports it")

    def _derive_stop(
        self,
        criterion_id: str,
        assertions: Mapping[str, _Assertion],
        reconciled: Mapping[str, CriterionResult],
    ) -> CriterionResult:
        durable: dict[str, CriterionResult] = {}
        for event in self.store.read_events():
            data = event.get("data")
            if event.get("type") != "acceptance.recorded" or not isinstance(data, dict):
                continue
            source_id = data.get("criterion_id")
            status = data.get("status")
            if not isinstance(source_id, str) or status not in {"pass", "fail", "blocked"}:
                continue
            durable[source_id] = CriterionResult(
                source_id,
                status,
                (f"events.ndjson#{event['event_id']}",) if status != "blocked" else (),
                data.get("note") if isinstance(data.get("note"), str) else None,
            )

        sources: list[CriterionResult] = []
        for source_id in _DERIVED_STOP_SOURCES[criterion_id]:
            result = reconciled.get(source_id) or durable.get(source_id)
            if result is None and (assertion := assertions.get(source_id)) is not None:
                result = CriterionResult(
                    source_id, assertion.status, assertion.evidence
                )
            if result is None:
                return self._blocked(
                    criterion_id, f"required stop-condition source is missing: {source_id}"
                )
            sources.append(result)
        evidence = tuple(
            dict.fromkeys(reference for result in sources for reference in result.evidence)
        )
        if any(result.status == "fail" for result in sources):
            return CriterionResult(
                criterion_id,
                "fail",
                evidence,
                "one or more authoritative stop-condition sources failed",
            )
        if any(result.status != "pass" for result in sources):
            return self._blocked(criterion_id, "a stop-condition source is not passing")
        return CriterionResult(criterion_id, "pass", evidence)

    def _execute_check(
        self, criterion_ids: tuple[str, ...], recorder: ActionRecorder
    ) -> StepResult:
        if criterion_ids != ("check.after-workflow-gates",):
            raise ValueError("Check phase must own exactly check.after-workflow-gates")
        workflow_ids = {
            item.criterion_id
            for item in original_acceptance_criteria()
            if item.criterion_id.startswith(("workflow1.", "workflow2.", "workflow3."))
        }
        recorded = {
            str(event["data"].get("criterion_id")): event["data"].get("status")
            for event in self.store.read_events()
            if event.get("type") == "acceptance.recorded"
            and isinstance(event.get("data"), dict)
        }
        if not workflow_ids or any(recorded.get(item) != "pass" for item in workflow_ids):
            event_id = self.store.append_event(
                "campaign.zeroth_check.refused",
                {"reason": "workflow_gates_not_all_passed"},
            )
            return StepResult(
                (
                    CriterionResult(
                        criterion_ids[0],
                        "fail",
                        (f"events.ndjson#{event_id}",),
                        "Zeroth Check was not run before all workflow gates passed",
                    ),
                )
            )
        if self.check_runner is None:
            event_id = self.store.append_event(
                "campaign.zeroth_check.refused", {"reason": "runner_not_configured"}
            )
            return StepResult(
                (
                    CriterionResult(
                        criterion_ids[0],
                        "fail",
                        (f"events.ndjson#{event_id}",),
                        "Zeroth Check runner is not configured",
                    ),
                )
            )
        observation = self.check_runner()
        evidence = recorder.record_command_result(
            name="zeroth-check",
            argv=observation.argv,
            working_directory=observation.working_directory,
            exit_code=observation.exit_code,
            stdout=observation.stdout,
            stderr=observation.stderr,
        )
        passed = observation.exit_code == 0 and observation.verdict == "pass"
        return StepResult(
            (
                CriterionResult(
                    criterion_ids[0],
                    "pass" if passed else "fail",
                    (evidence,),
                    None if passed else "Zeroth Check did not return a passing verdict",
                ),
            )
        )

    def _file_result(self, criterion_id: str, relative: str) -> CriterionResult:
        if not (self.store.root / relative).is_file():
            return self._blocked(criterion_id, f"{relative} does not exist")
        return CriterionResult(criterion_id, "pass", (relative,))

    @staticmethod
    def _blocked(criterion_id: str, note: str) -> CriterionResult:
        return CriterionResult(criterion_id, "blocked", (), note)
