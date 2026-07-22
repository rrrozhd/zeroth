import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { computeNextDelay } from "./usePolling";

describe("computeNextDelay", () => {
  it("returns base interval when active and visible", () => {
    expect(computeNextDelay({ active: true, hidden: false, base: 2000 })).toBe(2000);
  });
  it("returns null (pause) when inactive", () => {
    expect(computeNextDelay({ active: false, hidden: false, base: 2000 })).toBeNull();
  });
  it("backs off to 4x when tab hidden", () => {
    expect(computeNextDelay({ active: true, hidden: true, base: 2000 })).toBe(8000);
  });
});
