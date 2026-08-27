"""Command handlers for ``zeroth-core check``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from zeroth.check.config import load_check_config
from zeroth.check.reporting.terminal import render_terminal
from zeroth.check.reporting.writer import write_reports
from zeroth.check.tape.migrations import UnsupportedTapeVersionError, load_tape
from zeroth.check.tape.storage import RawRecordingStore, TapeStorageError
from zeroth.check.verdict.engine import reduce_verdict
from zeroth.check.verdict.models import (
    CheckEvidence,
    CheckStatus,
    CheckVerdict,
    FaultSummary,
    OrdinarySummary,
    PrerequisiteSummary,
    UsageSummary,
)
from zeroth.check.verdict.reasons import DESCRIPTIONS, ReasonCode

INVALID_EXIT = 30


def _ensure_working_directory_on_import_path() -> None:
    """Make local target modules importable in this process and spawned workers.

    Python console-script launchers put the virtualenv's ``bin`` directory at
    ``sys.path[0]`` instead of the caller's working directory. Check targets are
    deliberately application-owned modules, so a wheel-installed CLI must add
    the explicitly selected project directory before fresh-process rebuilds.
    """
    working_directory = str(Path.cwd().resolve())
    if working_directory not in sys.path:
        sys.path.insert(0, working_directory)


def _record(args: argparse.Namespace) -> int:
    from zeroth.check.adapter.recording import RecordingError, record_case

    try:
        _ensure_working_directory_on_import_path()
        config = load_check_config(args.config)
        from zeroth.integrations.langgraph import SQLiteActionExecutionRepository

        state_dir = Path(args.state_dir).resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        repository = SQLiteActionExecutionRepository(state_dir / "actions.sqlite")
        _, output = record_case(
            config.target,
            action_repository=repository,
            case=args.case,
            scenario_run_id=args.scenario_run_id or f"{args.case}-baseline",
            checkpointer_path=state_dir / "checkpoint.sqlite",
            store=RawRecordingStore(args.raw_dir),
            allow_side_effects=args.allow_side_effects,
        )
    except (OSError, ValidationError, RecordingError, TapeStorageError, ValueError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return INVALID_EXIT
    print(output)
    return 0


def _curate(args: argparse.Namespace) -> int:
    from zeroth.check.tape.curation import CurationError, curate_raw_recording

    try:
        result = curate_raw_recording(
            args.raw,
            output=args.output,
            reviewer_id=args.reviewer,
            overwrite=args.overwrite,
        )
    except (CurationError, OSError, ValidationError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return INVALID_EXIT
    print(f"approved {args.output} ({result.manifest.finding_count} secret findings scrubbed)")
    return 0


def _evidence(status: CheckStatus, code: ReasonCode, scope: str, summary: str) -> CheckEvidence:
    return CheckEvidence(status=status, reason_code=code, scope_key=scope, summary=summary)


def _run(args: argparse.Namespace) -> int:
    from zeroth.check.faults.engine import run_fault_matrix
    from zeroth.check.faults.models import MANDATORY_FAULTS
    from zeroth.check.replay.models import MismatchClassification, MismatchReason
    from zeroth.check.replay.runner import run_three

    try:
        _ensure_working_directory_on_import_path()
        config = load_check_config(args.config)
        paths = sorted(config.tapes.curated_dir.glob("*.json"))
        tapes = [load_tape(path) for path in paths]
        if args.case:
            selected = set(args.case)
            tapes = [tape for tape in tapes if tape.case_id in selected]
        if not tapes:
            raise ValueError("no approved tapes matched the selected cases")
    except (OSError, ValidationError, UnsupportedTapeVersionError, ValueError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return INVALID_EXIT

    report_dir = Path(args.report_dir).resolve()
    evidence: list[CheckEvidence] = []
    total_runs = total_matches = total_required_matches = 0
    required_faults = executed_faults = safety_violations = 0
    model_calls = 0
    usage_is_complete = True
    prerequisites_valid = True
    for tape in tapes:
        batch = run_three(
            config.target,
            tape,
            state_root=report_dir / "state" / tape.case_id / "ordinary",
        )
        total_runs += batch.quorum.total_runs
        total_matches += batch.quorum.matching_runs
        total_required_matches += batch.quorum.required_matches
        model_calls += len(tape.model_calls) * 3
        usage_is_complete = usage_is_complete and all(run.usage_complete for run in batch.runs)
        for run in batch.runs:
            if run.infrastructure_error:
                prerequisites_valid = False
                evidence.append(
                    _evidence(
                        CheckStatus.INVALID,
                        ReasonCode.INFRASTRUCTURE_FAILED,
                        f"{tape.case_id}:run-{run.slot}",
                        f"Replay worker failed: {run.infrastructure_error}",
                    )
                )
            for fact in run.facts:
                if fact.classification is MismatchClassification.BLOCK:
                    code = (
                        ReasonCode.ACTION_IDENTITY_MISMATCH
                        if fact.reason is MismatchReason.ACTION_IDENTITY_MISMATCH
                        else ReasonCode.LIVE_TOOL_ATTEMPTED
                        if fact.reason is MismatchReason.LIVE_TOOL_ATTEMPTED
                        else ReasonCode.UNSAFE_RETRY
                        if fact.reason is MismatchReason.DUPLICATE_SIDE_EFFECT
                        else ReasonCode.REPLAY_MISMATCH_SAFETY
                    )
                    evidence.append(
                        _evidence(
                            CheckStatus.BLOCK,
                            code,
                            f"{tape.case_id}:run-{run.slot}",
                            f"Fail-closed replay mismatch: {fact.reason.value}",
                        )
                    )
                elif fact.classification is MismatchClassification.INVALID:
                    prerequisites_valid = False
                    evidence.append(
                        _evidence(
                            CheckStatus.INVALID,
                            ReasonCode.TAPE_SCHEMA_INVALID,
                            f"{tape.case_id}:run-{run.slot}",
                            f"Replay prerequisite failed: {fact.reason.value}",
                        )
                    )
        if not all(run.full_check_eligible for run in batch.runs):
            prerequisites_valid = False
            evidence.append(
                _evidence(
                    CheckStatus.INVALID,
                    ReasonCode.TARGET_REBUILD_FAILED,
                    tape.case_id,
                    "Target did not request the harness action-repository seam",
                )
            )

        matrix = run_fault_matrix(
            tape,
            state_root=report_dir / "state" / tape.case_id / "faults",
            additional=list(config.faults.additional),
        )
        if not matrix.prerequisite_valid:
            prerequisites_valid = False
            evidence.append(
                _evidence(
                    CheckStatus.INVALID,
                    ReasonCode.NO_SIDE_EFFECTING_OCCURRENCE,
                    tape.case_id,
                    "No curated side-effecting occurrence is eligible for full Check",
                )
            )
        mandatory = [item for item in matrix.results if item.spec.name in MANDATORY_FAULTS]
        required_faults += len(mandatory)
        executed_faults += sum(item.executed for item in mandatory)
        safety_violations += sum(item.safety_violation for item in matrix.results)
        for result in matrix.results:
            scope = f"{tape.case_id}:{result.spec.action_identity}:{result.spec.name.value}"
            if result.spec.name in MANDATORY_FAULTS and not result.executed:
                prerequisites_valid = False
                evidence.append(
                    _evidence(
                        CheckStatus.INVALID,
                        ReasonCode.FAULT_NOT_OBSERVED,
                        scope,
                        "Mandatory injection and recovery points were not both observed",
                    )
                )
            if result.safety_violation:
                code = (
                    ReasonCode.RESTART_REEXECUTED
                    if result.spec.name.value == "restart_after_receipt"
                    else ReasonCode.DUPLICATE_EFFECT
                )
                evidence.append(
                    _evidence(
                        CheckStatus.BLOCK,
                        code,
                        scope,
                        "Fault execution observed more than one effect marker",
                    )
                )
            if result.spec.name not in MANDATORY_FAULTS and not result.executed:
                evidence.append(
                    _evidence(
                        CheckStatus.CANARY,
                        ReasonCode.OPTIONAL_FAULT_INCONCLUSIVE,
                        scope,
                        "Optional fault was inconclusive",
                    )
                )

    verdict = reduce_verdict(
        evidence,
        prerequisites=PrerequisiteSummary(valid=prerequisites_valid, cases=len(tapes)),
        ordinary=OrdinarySummary(
            runs=total_runs, matches=total_matches, required=total_required_matches
        ),
        faults=FaultSummary(
            required=required_faults,
            executed=executed_faults,
            safety_violations=safety_violations,
        ),
        usage=UsageSummary(model_calls=model_calls, complete=usage_is_complete),
    )
    write_reports(verdict, report_dir, fail_on_canary="canary" in config.reporting.fail_on)
    print(render_terminal(verdict), end="")
    return verdict.exit_code


def _explain(args: argparse.Namespace) -> int:
    try:
        verdict = CheckVerdict.model_validate_json(Path(args.verdict).read_bytes())
    except (OSError, ValidationError) as exc:
        print(f"invalid: not a CheckVerdict report: {exc}", file=sys.stderr)
        return INVALID_EXIT
    print(render_terminal(verdict), end="")
    for item in verdict.reasons:
        print(f"  {item.reason_code.value}: {DESCRIPTIONS[item.reason_code]}")
    return 0


def attach_check_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    check = subparsers.add_parser("check", help="record, curate, run, and explain Check tapes")
    commands = check.add_subparsers(dest="check_command", required=True)

    record = commands.add_parser("record", help="capture a raw target trajectory")
    record.add_argument("--case", required=True)
    record.add_argument("--scenario-run-id")
    record.add_argument("--config", default="zeroth-check.yaml")
    record.add_argument("--raw-dir", default=".zeroth/check/recordings")
    record.add_argument("--state-dir", default=".zeroth/check/state")
    record.add_argument("--allow-side-effects", action="store_true")
    record.set_defaults(func=_record)

    curate = commands.add_parser("curate", help="scrub and approve a raw recording")
    curate.add_argument("raw")
    curate.add_argument("--reviewer", required=True)
    curate.add_argument("--output", required=True)
    curate.add_argument("--overwrite", action="store_true")
    curate.set_defaults(func=_curate)

    run = commands.add_parser("run", help="run ordinary replay and mandatory faults")
    run.add_argument("--config", default="zeroth-check.yaml")
    run.add_argument("--case", action="append")
    run.add_argument("--report-dir", default=".zeroth/check/reports")
    run.set_defaults(func=_run)

    explain = commands.add_parser("explain", help="explain a saved CheckVerdict JSON report")
    explain.add_argument("verdict")
    explain.set_defaults(func=_explain)
