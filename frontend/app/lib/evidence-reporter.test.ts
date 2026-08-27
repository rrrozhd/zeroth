import { describe, expect, it } from "vitest";

import {
  artifactFilename,
  buildEvidenceIndex,
  type IndexedTestResult,
} from "../../e2e/support/evidence-reporter";

describe("artifactFilename", () => {
  it("qualifies otherwise identical evidence by browser project", () => {
    const chromium = artifactFilename(
      "desktop-1440",
      "same-test",
      0,
      "resilient-http-summary",
      "application/json",
    );
    const webkit = artifactFilename(
      "webkit-1440",
      "same-test",
      0,
      "resilient-http-summary",
      "application/json",
    );

    expect(chromium).toMatch(/^desktop-1440-/);
    expect(webkit).toMatch(/^webkit-1440-/);
    expect(chromium).not.toBe(webkit);
  });
});

describe("buildEvidenceIndex", () => {
  it("fails a criterion when any actual annotated Playwright result fails", () => {
    const results: IndexedTestResult[] = [
      {
        testId: "a",
        title: "desktop route",
        status: "passed",
        criteria: ["ui.viewport-1440x900"],
        artifacts: [
          { source: "artifacts/a.png", destination: "screenshots/a.png" },
        ],
      },
      {
        testId: "b",
        title: "desktop route regression",
        status: "failed",
        criteria: ["ui.viewport-1440x900"],
        artifacts: [
          { source: "console/b.json", destination: "console/b.json" },
        ],
      },
    ];

    const index = buildEvidenceIndex(results, true);

    expect(index.completed).toBe(true);
    expect(index.criteria).toEqual([
      {
        criterion_id: "ui.viewport-1440x900",
        status: "fail",
        test_id: "a,b",
        evidence: ["console/b.json", "screenshots/a.png"],
      },
    ]);
  });

  it("omits skipped assertions so the Python gate remains blocked", () => {
    const index = buildEvidenceIndex(
      [
        {
          testId: "skipped",
          title: "not executed",
          status: "skipped",
          criteria: ["ui.node-placement"],
          artifacts: [],
        },
      ],
      true,
    );

    expect(index.criteria).toEqual([]);
  });
});
