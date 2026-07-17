import { describe, it, expect } from "vitest";
import { regulusStatusFrom } from "./regulus";

describe("regulusStatusFrom", () => {
  it("enabled on 2xx (sub-app mounted, served the authed probe)", () => {
    expect(regulusStatusFrom(200)).toBe("enabled");
    expect(regulusStatusFrom(204)).toBe("enabled");
  });
  it("absent on 403/503/404 (not admin / mount off / route absent → hidden)", () => {
    expect(regulusStatusFrom(403)).toBe("absent");
    expect(regulusStatusFrom(503)).toBe("absent");
    expect(regulusStatusFrom(404)).toBe("absent");
  });
  it("unknown on 401/network (indeterminate → hidden)", () => {
    expect(regulusStatusFrom(401)).toBe("unknown");
  });
});
