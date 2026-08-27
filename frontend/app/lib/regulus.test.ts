import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./config", () => ({ getApiBase: () => "https://zeroth.test", getApiKey: () => "key" }));
const api = vi.hoisted(() => ({
  getIdentity: vi.fn().mockResolvedValue({ roles: ["admin"] }),
}));
vi.mock("./api", () => api);

import { detectRegulus, regulusStatusFrom } from "./regulus";

describe("regulusStatusFrom", () => {
  it("enables only successful authenticated probes", () => {
    expect(regulusStatusFrom(200)).toBe("enabled");
    expect(regulusStatusFrom(204)).toBe("enabled");
  });

  it("hides absent, unavailable, and unauthorized Regulus", () => {
    expect(regulusStatusFrom(403)).toBe("absent");
    expect(regulusStatusFrom(404)).toBe("absent");
    expect(regulusStatusFrom(503)).toBe("absent");
    expect(regulusStatusFrom(401)).toBe("unknown");
  });
});

describe("detectRegulus", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    api.getIdentity.mockResolvedValue({ roles: ["admin"] });
  });
  afterEach(() => vi.unstubAllGlobals());

  it.each([[200, "enabled"], [403, "absent"], [404, "absent"]] as const)(
    "classifies authenticated HTTP %i as %s",
    async (status, expected) => {
      vi.mocked(fetch).mockResolvedValue({ status } as Response);
      await expect(detectRegulus()).resolves.toBe(expected);
      expect(fetch).toHaveBeenCalledWith(
        "https://zeroth.test/v1/econ/regulus/dashboard/kpis",
        expect.objectContaining({ headers: { "X-API-Key": "key" } }),
      );
    },
  );

  it("hides the group on transport failure", async () => {
    vi.mocked(fetch).mockRejectedValue(new Error("offline"));
    await expect(detectRegulus()).resolves.toBe("unknown");
  });

  it.each(["operator", "reviewer"])(
    "does not make a predictably forbidden Regulus request for %s",
    async (role) => {
      api.getIdentity.mockResolvedValue({ roles: [role] });

      await expect(detectRegulus()).resolves.toBe("absent");

      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("lets the authoritative API decide access for a configured custom role", async () => {
    api.getIdentity.mockResolvedValue({ roles: ["economics_manager"] });
    vi.mocked(fetch).mockResolvedValue({ status: 200 } as Response);

    await expect(detectRegulus()).resolves.toBe("enabled");

    expect(fetch).toHaveBeenCalledWith(
      "https://zeroth.test/v1/econ/regulus/dashboard/kpis",
      expect.objectContaining({ headers: { "X-API-Key": "key" } }),
    );
  });

  it("fails closed without probing Regulus when identity is unavailable", async () => {
    api.getIdentity.mockRejectedValue(new Error("identity unavailable"));

    await expect(detectRegulus()).resolves.toBe("unknown");

    expect(fetch).not.toHaveBeenCalled();
  });
});
