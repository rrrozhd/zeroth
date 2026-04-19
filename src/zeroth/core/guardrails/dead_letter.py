"""Dead-letter manager: marks repeatedly-failing runs as unrecoverable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from zeroth.core.runs import RunFailureState, RunRepository, RunStatus
from zeroth.core.runs.repository import DEAD_LETTER_REASON


@dataclass(slots=True)
class DeadLetterManager:
    """Escalates a run to dead-letter status after too many failures.

    When ``handle_run_failure`` is called the failure_count is incremented.
    If the count reaches ``max_failure_count`` the run is transitioned to FAILED
    with ``failure_state.reason = "dead_letter"`` and an admin must explicitly
    replay it via the admin API.
    """

    run_repository: RunRepository
    max_failure_count: int = 3

    async def handle_run_failure(self, run_id: str) -> bool:
        """Increment failure_count and dead-letter the run if threshold reached.

        Returns True if the run was dead-lettered, False otherwise.
        """
        new_count = await self.run_repository.increment_failure_count(run_id)
        if new_count < self.max_failure_count:
            return False

        run = await self.run_repository.get(run_id)
        if run is None:
            return False
        if run.status in {RunStatus.COMPLETED}:
            return False

        run.failure_state = RunFailureState(
            reason=DEAD_LETTER_REASON,
            message=f"run failed {new_count} times and was dead-lettered",
            details={"failure_count": new_count, "dead_lettered_at": datetime.now(UTC).isoformat()},
        )
        # Transition to FAILED (covers both RUNNING and PENDING states).
        if run.status not in {RunStatus.FAILED}:
            try:
                run = await self.run_repository.transition(
                    run_id,
                    RunStatus.FAILED,
                    failure_state=run.failure_state,
                )
            except (ValueError, KeyError):
                run.status = RunStatus.FAILED
                run.touch()
                await self.run_repository.put(run)
        else:
            run.touch()
            await self.run_repository.put(run)

        return True
