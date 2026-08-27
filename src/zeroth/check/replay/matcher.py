"""Identity-matched pool of unused approved tool occurrences."""

from __future__ import annotations

from threading import RLock
from typing import Any

from zeroth.check.replay.models import (
    CLASSIFICATION,
    MismatchReason,
    ReplayFact,
    ReplayFinish,
    ReplayMismatchError,
)
from zeroth.check.tape.models import TapeV1
from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint


class ReplayMatcher:
    """Validate calls against a tape and return only recorded results."""

    def __init__(self, tape: TapeV1) -> None:
        self._tape = tape
        self._occurrences = tuple(tape.tool_occurrences)
        self._unused = set(range(len(self._occurrences)))
        self._observed: list[int] = []
        self._lock = RLock()

    def validate_registration(self, name: str, schema_digest: str) -> None:
        expected = [item for item in self._occurrences if item.name == name]
        if not expected:
            raise ReplayMismatchError(MismatchReason.UNKNOWN_TOOL, actual_fingerprint=schema_digest)
        expected_digests = {item.input_schema_digest for item in expected}
        if expected_digests != {schema_digest}:
            raise ReplayMismatchError(
                MismatchReason.SCHEMA_DIGEST_MISMATCH,
                expected_fingerprint=sorted(expected_digests)[0],
                actual_fingerprint=schema_digest,
            )

    def call(
        self,
        *,
        name: str,
        schema_digest: str,
        tool_call_id: str | None,
        arguments: dict[str, Any],
    ) -> Any:
        with self._lock:
            self.validate_registration(name, schema_digest)
            argument_digest = argument_fingerprint(arguments)
            if not tool_call_id:
                raise ReplayMismatchError(
                    MismatchReason.TOOL_CALL_ID_MISMATCH,
                    actual_fingerprint=argument_digest,
                )
            named = [
                (index, item)
                for index, item in enumerate(self._occurrences)
                if item.name == name and item.input_schema_digest == schema_digest
            ]
            matching_id = [
                (index, item) for index, item in named if item.tool_call_id == tool_call_id
            ]
            if not matching_id:
                raise ReplayMismatchError(
                    MismatchReason.TOOL_CALL_ID_MISMATCH,
                    expected_fingerprint=named[0][1].action_identity,
                    actual_fingerprint=argument_digest,
                )
            index, occurrence = matching_id[0]
            if index not in self._unused:
                reason = (
                    MismatchReason.DUPLICATE_SIDE_EFFECT
                    if occurrence.side_effect == "side_effecting"
                    else MismatchReason.EXTRA_CALL
                )
                raise ReplayMismatchError(reason, expected_fingerprint=occurrence.action_identity)
            if occurrence.argument_fingerprint != argument_digest:
                raise ReplayMismatchError(
                    MismatchReason.ARGUMENT_MISMATCH,
                    expected_fingerprint=occurrence.argument_fingerprint,
                    actual_fingerprint=argument_digest,
                )
            actual_identity = action_identity_v1(
                case_id=self._tape.case_id,
                scenario_run_id=self._tape.scenario_run_id,
                tool_name=name,
                input_schema_digest=schema_digest,
                tool_call_id=tool_call_id,
                argument_fingerprint=argument_digest,
            )
            if occurrence.action_identity != actual_identity:
                raise ReplayMismatchError(
                    MismatchReason.ACTION_IDENTITY_MISMATCH,
                    expected_fingerprint=occurrence.action_identity,
                    actual_fingerprint=actual_identity,
                )
            if not occurrence.result_available:
                raise ReplayMismatchError(
                    MismatchReason.MISSING_RESULT,
                    expected_fingerprint=occurrence.action_identity,
                )
            self._unused.remove(index)
            self._observed.append(index)
            return occurrence.result

    def finish(self) -> ReplayFinish:
        with self._lock:
            facts: list[ReplayFact] = []
            if self._unused:
                facts.append(
                    ReplayFact(
                        reason=MismatchReason.EARLY_END,
                        classification=CLASSIFICATION[MismatchReason.EARLY_END],
                        expected_fingerprint=self._occurrences[min(self._unused)].action_identity,
                    )
                )
            expected_observed_order = sorted(self._observed)
            if self._observed and self._observed != expected_observed_order:
                facts.append(
                    ReplayFact(
                        reason=MismatchReason.CHANGED_ORDER,
                        classification=CLASSIFICATION[MismatchReason.CHANGED_ORDER],
                    )
                )
            return ReplayFinish(
                facts=tuple(facts),
                observed_action_identities=tuple(
                    self._occurrences[index].action_identity for index in self._observed
                ),
            )

    @property
    def observed_occurrences(self) -> tuple[object, ...]:
        with self._lock:
            return tuple(self._occurrences[index] for index in self._observed)
