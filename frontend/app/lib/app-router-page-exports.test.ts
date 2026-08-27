import { describe, expect, it } from "vitest";

describe("Next App Router page exports", () => {
  it.each([
    ["connectors", () => import("../connectors/page")],
    ["templates", () => import("../templates/page")],
    ["webhooks", () => import("../webhooks/page")],
  ])("keeps %s page exports inside the Next page contract", async (_name, loadPage) => {
    expect(Object.keys(await loadPage()).sort()).toEqual(["default"]);
  });
});
