import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WebhookSummary } from "./webhook-summary";

describe("WebhookSummary", () => {
  it("routes webhook administration to the canonical surface", () => {
    const html = renderToStaticMarkup(<WebhookSummary />);

    expect(html).toContain('href="/console/webhooks/"');
    expect(html).toContain("Manage webhooks");
    expect(html).not.toContain("New subscription");
  });
});
