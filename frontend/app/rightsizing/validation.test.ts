import { describe, expect, it } from "vitest";

import { validateSuggestBounds } from "./validation";

describe("validateSuggestBounds", () => {
  it("accepts blank values and both documented boundaries", () => {
    expect(validateSuggestBounds("", "")).toEqual({});
    expect(validateSuggestBounds("0", "1")).toEqual({ minSavingsPct: 0, limit: 1 });
    expect(validateSuggestBounds("100", "20")).toEqual({ minSavingsPct: 100, limit: 20 });
  });

  it.each([
    ["-0.1", "", "min_savings_pct"],
    ["100.1", "", "min_savings_pct"],
    ["NaN", "", "min_savings_pct"],
    ["", "0", "limit"],
    ["", "21", "limit"],
    ["", "1.5", "limit"],
  ])("rejects invalid bounds without sending them", (savings, limit, field) => {
    expect(validateSuggestBounds(savings, limit)).toMatchObject({ invalidField: field });
  });
});

describe("validateExperimentOptions", () => {
  it("accepts blanks and the documented numeric boundaries", async () => {
    const validation = await import("./validation");
    const validateExperimentOptions = (
      validation as typeof validation & {
        validateExperimentOptions?: (
          tolerance: string,
          maxCases: string,
          maxCandidates: string,
          minCases: string,
        ) => unknown;
      }
    ).validateExperimentOptions;

    expect(validateExperimentOptions?.("", "", "", "")).toEqual({});
    expect(validateExperimentOptions?.("0", "1", "1", "1")).toEqual({
      tolerancePct: 0,
      maxCases: 1,
      maxCandidates: 1,
      minCases: 1,
    });
    expect(validateExperimentOptions?.("100", "25", "6", "50")).toEqual({
      tolerancePct: 100,
      maxCases: 25,
      maxCandidates: 6,
      minCases: 50,
    });
  });

  it.each([
    ["-0.1", "", "", "", "tolerance_pct"],
    ["100.1", "", "", "", "tolerance_pct"],
    ["not-a-number", "", "", "", "tolerance_pct"],
    ["", "0", "", "", "max_cases"],
    ["", "26", "", "", "max_cases"],
    ["", "1.5", "", "", "max_cases"],
    ["", "", "0", "", "max_candidates"],
    ["", "", "7", "", "max_candidates"],
    ["", "", "1.5", "", "max_candidates"],
    ["", "", "", "0", "min_cases"],
    ["", "", "", "51", "min_cases"],
    ["", "", "", "1.5", "min_cases"],
  ])(
    "rejects an invalid experiment bound instead of omitting it",
    async (tolerance, maxCases, maxCandidates, minCases, invalidField) => {
      const { validateExperimentOptions } = await import("./validation");
      expect(validateExperimentOptions(tolerance, maxCases, maxCandidates, minCases)).toMatchObject({
        invalidField,
      });
    },
  );
});
