import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { VERSION } from "./version";

/**
 * `version.ts` is generated from `pyproject.toml` by
 * `scripts/generate_frontend_version.py`, so the assertion reads the same source
 * the generator does.
 *
 * It used to compare against the literal `"0.22"`. A literal cannot track a
 * version that moves, and this one had not: the package reached 0.22.1.1.1 and
 * the console suite was red, unnoticed, because no pull-request workflow runs it
 * — the Python gate does not, and `release-gates.yml` is nightly plus release.
 * A frozen expectation drifting away from the thing it names is the defect
 * ZER-41 exists to remove, so it is removed here too.
 */
function packageVersion(): string {
  const pyproject = readFileSync(join(__dirname, "../../../pyproject.toml"), "utf8");
  const match = pyproject.match(/^version\s*=\s*"([^"]+)"/m);
  if (!match) {
    throw new Error("pyproject.toml declares no project version");
  }
  return match[1];
}

describe("console version", () => {
  it("matches the integrated Python package version", () => {
    expect(VERSION).toBe(packageVersion());
  });

  it("reads a version that is not a frozen literal", () => {
    // Guards the guard: a `packageVersion()` that silently returned the same
    // string as `VERSION` would make the assertion above vacuous.
    expect(packageVersion()).toMatch(/^\d+(\.\d+)+$/);
    expect(packageVersion()).not.toBe("0.22");
  });
});
