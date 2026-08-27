import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const specPath = resolve(
  import.meta.dirname,
  "../../e2e/retention-visual-accessibility-live.spec.ts",
);

describe("Retention visual accessibility live contract", () => {
  it("pins the authorized five-project evidence matrix without live mutations", () => {
    expect(existsSync(specPath), "dedicated Retention visual acceptance spec is required").toBe(true);
    const source = existsSync(specPath) ? readFileSync(specPath, "utf8") : "";

    for (const required of [
      "ZEROTH_EVALUATION_ROLE_SECRET_ROOT",
      "service-api-key",
      "/v1/identity",
      "/health",
      "platform_admin",
      "retention.policy.card",
      "retention.legal-holds.card",
      "retention.erasure.card",
      "releaseButtons",
      "toHaveCount(1)",
      "assertVisibleAndNotClipped",
      "assertEnabledTargetSizes",
      "wcag22aa",
      'page.on("pageerror"',
      "unhandledrejection",
      "retention-keyboard-focus",
      "retention-sanitized-network",
      "retention-visual-accessibility",
      "retention-zoom-200",
      "product.retention.responsive-and-zoom",
      "product.retention.webkit-axe-and-keyboard",
    ]) {
      expect(source, `missing acceptance seam: ${required}`).toContain(required);
    }

    expect(source).not.toContain("request.post(");
    expect(source).not.toContain("request.put(");
    expect(source).not.toContain("request.delete(");
    expect(source).not.toContain('test.skip(testInfo.project.name !== "desktop-1440"');
  });
});
