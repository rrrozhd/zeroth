import { describe, it, expect } from "vitest";
import { regulusStatusFrom } from "./regulus";

describe("regulusStatusFrom", () => {
  it("enabled on 200", () => expect(regulusStatusFrom(200)).toBe("enabled"));
  it("enabled on 401/403 (mounted but auth-gated)", () => {
    expect(regulusStatusFrom(401)).toBe("enabled");
    expect(regulusStatusFrom(403)).toBe("enabled");
  });
  it("absent on 404", () => expect(regulusStatusFrom(404)).toBe("absent"));
});
