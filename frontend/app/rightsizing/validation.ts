export type SuggestBounds = {
  minSavingsPct?: number;
  limit?: number;
  error?: string;
  invalidField?: "min_savings_pct" | "limit";
};

export type ExperimentOptions = {
  tolerancePct?: number;
  maxCases?: number;
  maxCandidates?: number;
  minCases?: number;
  error?: string;
  invalidField?: "tolerance_pct" | "max_cases" | "max_candidates" | "min_cases";
};

/** Parse the exact backend bounds instead of silently dropping invalid input. */
export function validateSuggestBounds(minSavings: string, limit: string): SuggestBounds {
  const parsed: SuggestBounds = {};
  const savingsText = minSavings.trim();
  if (savingsText) {
    const value = Number(savingsText);
    if (!Number.isFinite(value) || value < 0 || value > 100) {
      return {
        error: "Minimum savings must be a number from 0 through 100.",
        invalidField: "min_savings_pct",
      };
    }
    parsed.minSavingsPct = value;
  }

  const limitText = limit.trim();
  if (limitText) {
    const value = Number(limitText);
    if (!Number.isInteger(value) || value < 1 || value > 20) {
      return {
        error: "Limit must be a whole number from 1 through 20.",
        invalidField: "limit",
      };
    }
    parsed.limit = value;
  }
  return parsed;
}

/** Parse the measured experiment's optional numeric fields using API bounds. */
export function validateExperimentOptions(
  tolerance: string,
  maxCases: string,
  maxCandidates: string,
  minCases: string,
): ExperimentOptions {
  const parsed: ExperimentOptions = {};
  const values = [
    ["tolerance_pct", tolerance, 0, 100, false, "Tolerance must be a number from 0 through 100."],
    ["max_cases", maxCases, 1, 25, true, "Maximum cases must be a whole number from 1 through 25."],
    ["max_candidates", maxCandidates, 1, 6, true, "Maximum candidates must be a whole number from 1 through 6."],
    ["min_cases", minCases, 1, 50, true, "Minimum cases must be a whole number from 1 through 50."],
  ] as const;

  for (const [field, input, minimum, maximum, integer, message] of values) {
    const text = input.trim();
    if (!text) continue;
    const value = Number(text);
    if (!Number.isFinite(value) || (integer && !Number.isInteger(value)) || value < minimum || value > maximum) {
      return { error: message, invalidField: field };
    }
    if (field === "tolerance_pct") parsed.tolerancePct = value;
    if (field === "max_cases") parsed.maxCases = value;
    if (field === "max_candidates") parsed.maxCandidates = value;
    if (field === "min_cases") parsed.minCases = value;
  }
  return parsed;
}
