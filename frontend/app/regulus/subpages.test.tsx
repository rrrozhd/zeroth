import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import CostingPage from "./costing/page";
import EnforcementPage from "./enforcement/page";
import ReconciliationPage from "./reconciliation/page";

describe("Economics subpage shells", () => {
  it.each([
    ["Costing", CostingPage, "Not connected"],
    ["Enforcement", EnforcementPage, "Not connected"],
    ["Reconciliation", ReconciliationPage, "Calibration summary"],
  ])("renders %s with the shared page hierarchy and a safe initial state", (title, Page, initialState) => {
    const html = renderToStaticMarkup(<Page />);

    expect(html).toContain("<h1");
    expect(html).toContain(title);
    expect(html).toContain(initialState);
    expect(html).not.toContain("rgba(248,113,113");
  });
});
