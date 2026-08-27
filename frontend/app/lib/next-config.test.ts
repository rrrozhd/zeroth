import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

describe("persistent development route cache", () => {
  it("keeps the full published console surface warm during validation", () => {
    expect(nextConfig.onDemandEntries?.pagesBufferLength).toBeGreaterThanOrEqual(21);
    expect(nextConfig.onDemandEntries?.maxInactiveAge).toBeGreaterThanOrEqual(15 * 60 * 1000);
  });
});
