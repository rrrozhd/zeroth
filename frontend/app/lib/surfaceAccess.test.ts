import { describe, expect, it } from "vitest";

import { surfaceAccessMessage } from "./surfaceAccess";

describe("surfaceAccessMessage", () => {
  it("explains role-gated mixed surfaces without presenting them as outages", () => {
    expect(surfaceAccessMessage("403 forbidden", "Webhooks")).toBe(
      "Webhooks are hidden because this API key cannot read webhook administration data.",
    );
  });

  it("preserves actionable non-authorization failures", () => {
    expect(surfaceAccessMessage("503 delivery store unavailable", "Webhooks")).toBe(
      "503 delivery store unavailable",
    );
  });
});
