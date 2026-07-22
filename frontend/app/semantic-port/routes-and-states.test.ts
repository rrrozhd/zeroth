import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { ROUTE_CASES, STATE_CASE_IDS } from "./source-parity-cases";

const root = resolve(import.meta.dirname, "../..");

describe("source route and state parity", () => {
  it.each(ROUTE_CASES)("$id has a route module", ({ file }) => {
    expect(existsSync(resolve(root, file))).toBe(true);
  });

  it("maps every state contract to executable source evidence", () => {
    const asyncSource = ROUTE_CASES.filter((item) => item.asyncStates)
      .filter((item) => existsSync(resolve(root, item.file)))
      .map((item) => readFileSync(resolve(root, item.file), "utf8"))
      .join("\n");
    const evidence: Record<(typeof STATE_CASE_IDS)[number], boolean> = {
      "state-loading": /loading|Skeleton/.test(asyncSource),
      "state-empty": /empty|No /.test(asyncSource),
      "state-error": /error|Error/.test(asyncSource),
      "state-connect": /Connect/.test(asyncSource),
      "state-regulus-unavailable": /Regulus/.test(asyncSource),
      "state-regulus-404-empty": /404/.test(asyncSource),
      "state-metrics-text": /Prometheus|text\(\)/.test(asyncSource),
      "state-audit-signature": /signature|unsigned/.test(asyncSource),
    };
    expect(Object.keys(evidence).sort()).toEqual([...STATE_CASE_IDS].sort());
    expect(Object.values(evidence).every(Boolean)).toBe(true);
  });
});
