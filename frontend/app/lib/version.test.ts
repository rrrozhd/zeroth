import { describe, expect, it } from "vitest";

import { VERSION } from "./version";

describe("console version", () => {
  it("matches the integrated Python package version", () => {
    expect(VERSION).toBe("0.16.1.0.2");
  });
});
