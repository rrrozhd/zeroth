import { describe, it, expect } from "vitest";
import { regulusStatusFrom } from "./regulus";

describe("regulusStatusFrom", () => {
  it("enabled on 2xx (sub-app mounted, served the authed probe)", () => {
    expect(regulusStatusFrom(200)).toBe("enabled");
    expect(regulusStatusFrom(204)).toBe("enabled");
  });
  it("absent on 404 (auth passed, nothing mounted at /regulus)", () => {
    expect(regulusStatusFrom(404)).toBe("absent");
  });
  it("unknown on 401/403 (missing/invalid key or role — can't tell)", () => {
    // The whole service is behind auth middleware, so 401/403 does NOT imply a
    // mounted-but-gated sub-app; treat it as indeterminate and hide the group.
    expect(regulusStatusFrom(401)).toBe("unknown");
    expect(regulusStatusFrom(403)).toBe("unknown");
  });
});
