export type EconomicsReconciliation = {
  ledgerActualUsd: number;
  runAttributedUsd: number;
  differenceUsd: number;
  withinTolerance: boolean;
  explanation: string;
};

export function budgetFailureModeCopy(failureMode: string | null | undefined): string {
  if (failureMode === "fail_closed") {
    return "Fail-closed: new provider spend is denied when Regulus cannot authorize it.";
  }
  if (failureMode === "fail_open") {
    return "Fail-open: provider spend may proceed when Regulus is unavailable.";
  }
  return "Failure mode unavailable until effective economics configuration loads.";
}

/**
 * Compare the month-to-date production ledger with the bounded run/audit view.
 *
 * These are intentionally not forced to match: provider verification and connector
 * probes are real deployment spend without a workflow run, while a latest-200-run
 * window can include runs outside the current calendar month. The result names that
 * difference instead of silently assigning operation-level spend to a workflow.
 */
export function reconcileEconomics(
  ledgerActualUsd: number,
  runAttributedUsd: number,
): EconomicsReconciliation {
  const ledger = Number.isFinite(ledgerActualUsd) ? ledgerActualUsd : 0;
  const attributed = Number.isFinite(runAttributedUsd) ? runAttributedUsd : 0;
  const difference = ledger - attributed;
  const tolerance = Math.max(0.000001, Math.abs(ledger) * 0.005);
  const withinTolerance = Math.abs(difference) <= tolerance;

  let explanation = "The production ledger and bounded run attribution reconcile within tolerance.";
  if (!withinTolerance && difference > 0) {
    explanation =
      "The difference is real deployment spend outside workflow-run attribution, such as provider verification or connector probes, or spend outside the latest run window.";
  } else if (!withinTolerance && difference < 0) {
    explanation =
      "The latest run window includes attributed spend outside the current month-to-date ledger window.";
  }

  return {
    ledgerActualUsd: ledger,
    runAttributedUsd: attributed,
    differenceUsd: difference,
    withinTolerance,
    explanation,
  };
}
