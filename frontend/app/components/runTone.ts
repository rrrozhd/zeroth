// Run lifecycle status -> primitive tone. Shared by Overview, Runs, and the
// sidebar so status colors stay consistent. Terminal-bad states read danger;
// the live "running" state pulses (callers pass `pulse={status==="running"}`).
export const RUN_TONE: Record<string, string> = {
  queued: "warning",
  running: "info",
  paused_for_approval: "warning",
  waiting_interrupt: "warning",
  succeeded: "success",
  failed: "danger",
  terminated_by_policy: "danger",
  terminated_by_loop_guard: "danger",
  dead_letter: "danger",
};

/** Human label for a run status ("paused_for_approval" -> "paused for approval"). */
export function runStatusLabel(status: string): string {
  return status.replace(/_/g, " ");
}
