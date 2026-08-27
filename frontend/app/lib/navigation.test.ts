import { describe, expect, it } from "vitest";

import { ECONOMICS_VIEWS, NAV, TITLE } from "../components/nav";
import { ROUTE_CASES } from "../semantic-port/source-parity-cases";

describe("console navigation parity", () => {
  it("makes tenant-scoped artifacts directly discoverable from Operate", () => {
    const operate = NAV.find((group) => group.heading === "Operate");
    expect(operate?.items).toContainEqual({ label: "Artifacts", href: "/artifacts" });
    expect(TITLE["/artifacts"]).toBe("Artifacts");
  });

  it("contains every source and destination-only route", () => {
    const hrefs = new Set([
      ...NAV.flatMap((group) => group.items.map((item) => item.href)),
      ...ECONOMICS_VIEWS.map((view) => view.href),
    ]);
    const navigableRoutes = ROUTE_CASES.filter((item) => item.href !== "/studio/edit");
    expect([...hrefs].sort()).toEqual(navigableRoutes.map((item) => item.href).sort());
    for (const route of navigableRoutes) expect(TITLE[route.href]).toBeTruthy();
  });

  it("keeps restricted economics views discoverable and explains access in place", () => {
    expect(NAV.filter((group) => group.gated)).toEqual([]);
    expect(ECONOMICS_VIEWS.map((view) => view.access)).toEqual([
      "metrics_read",
      "metrics_read",
      "metrics_read",
      "metrics_read",
    ]);
    expect(ECONOMICS_VIEWS.map((view) => view.href)).toEqual([
      "/cost",
      "/regulus",
      "/regulus/costing",
      "/regulus/reconciliation",
    ]);
  });
});
