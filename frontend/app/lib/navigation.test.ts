import { describe, expect, it } from "vitest";

import { NAV, TITLE } from "../components/nav";
import { ROUTE_CASES } from "../semantic-port/source-parity-cases";

describe("console navigation parity", () => {
  it("contains every source and destination-only route", () => {
    const hrefs = new Set(NAV.flatMap((group) => group.items.map((item) => item.href)));
    const navigableRoutes = ROUTE_CASES.filter((item) => item.href !== "/studio/edit");
    expect([...hrefs].sort()).toEqual(navigableRoutes.map((item) => item.href).sort());
    for (const route of navigableRoutes) expect(TITLE[route.href]).toBeTruthy();
  });

  it("gates only the Regulus group", () => {
    expect(NAV.filter((group) => group.gated).map((group) => group.heading)).toEqual(["Regulus"]);
  });
});
