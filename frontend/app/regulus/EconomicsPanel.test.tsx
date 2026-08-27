import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { EconomicsPanel } from "./EconomicsPanel";

describe("EconomicsPanel", () => {
  it("uses the shared Console section and surface hierarchy", () => {
    const html = renderToStaticMarkup(
      <EconomicsPanel title="Policy timeline" density="compact">
        <p>Approved policy</p>
      </EconomicsPanel>,
    );

    expect(html).toContain("<section");
    expect(html).toContain("<h2");
    expect(html).toContain("Policy timeline");
    expect(html).toContain("Approved policy");
  });
});
