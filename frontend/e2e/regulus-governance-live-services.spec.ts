import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  assertAccessibility,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const secretRoot = process.env.ZEROTH_EVALUATION_SECRET_ROOT;

type LiveFixture = {
  tenant: string;
  apiBase: string;
  role: "operator" | "reviewer" | "admin" | "platform_admin";
  secretName: string;
  canRead: boolean;
  canMutate: boolean;
  expectedCapabilities: number;
  expectedActions: number;
};

const fixtures: LiveFixture[] = [
  {
    tenant: "evaluation-studio-v1",
    apiBase: "http://127.0.0.1:8122",
    role: "operator",
    secretName: "tenant-a-operator-key",
    canRead: false,
    canMutate: false,
    expectedCapabilities: 0,
    expectedActions: 0,
  },
  {
    tenant: "evaluation-studio-v1",
    apiBase: "http://127.0.0.1:8122",
    role: "reviewer",
    secretName: "tenant-a-reviewer-key",
    canRead: false,
    canMutate: false,
    expectedCapabilities: 0,
    expectedActions: 0,
  },
  {
    tenant: "evaluation-studio-v1",
    apiBase: "http://127.0.0.1:8122",
    role: "platform_admin",
    secretName: "service-api-key",
    canRead: true,
    canMutate: true,
    expectedCapabilities: 3,
    expectedActions: 3,
  },
  {
    tenant: "evaluation-studio-v1-twin",
    apiBase: "http://127.0.0.1:8123",
    role: "operator",
    secretName: "tenant-b-operator-key",
    canRead: false,
    canMutate: false,
    expectedCapabilities: 0,
    expectedActions: 0,
  },
  {
    tenant: "evaluation-studio-v1-twin",
    apiBase: "http://127.0.0.1:8123",
    role: "reviewer",
    secretName: "tenant-b-reviewer-key",
    canRead: false,
    canMutate: false,
    expectedCapabilities: 0,
    expectedActions: 0,
  },
  {
    tenant: "evaluation-studio-v1-twin",
    apiBase: "http://127.0.0.1:8123",
    role: "admin",
    secretName: "tenant-b-admin-key",
    canRead: true,
    canMutate: false,
    expectedCapabilities: 1,
    expectedActions: 0,
  },
  {
    tenant: "evaluation-studio-v1-twin",
    apiBase: "http://127.0.0.1:8123",
    role: "platform_admin",
    secretName: "tenant-b-platform-admin-key",
    canRead: true,
    canMutate: true,
    expectedCapabilities: 1,
    expectedActions: 0,
  },
];

function secret(name: string): string {
  if (!secretRoot) throw new Error("ZEROTH_EVALUATION_SECRET_ROOT is required");
  return readFileSync(join(secretRoot, name), "utf8").trim();
}
for (const fixture of fixtures) {
  test(`live ${fixture.tenant} ${fixture.role} governance surfaces`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    test.skip(!liveEnabled || !secretRoot, "requires the frozen local evaluation services and external secrets");
    test.skip(
      !["desktop-1440", "webkit-1440"].includes(testInfo.project.name),
      "governance live-service acceptance runs in Chromium and WebKit",
    );
    coverCriteria(
      testInfo,
      "regulus.capabilities.authorization.live",
      "regulus.enforcement.authorization.live",
      `identity.role.${fixture.role}.live`,
    );

    const protectedRequests: string[] = [];
    page.on("request", (request) => {
      const pathname = new URL(request.url()).pathname;
      if (
        pathname === "/v1/econ/regulus/registry/capabilities"
        || pathname === "/v1/econ/regulus/enforcement/actions"
      ) protectedRequests.push(pathname);
    });

    const browserEvidence = new BrowserEvidence(page, new URL(fixture.apiBase).origin);
    await configurePage(page, fixture.apiBase, fixture.tenant, secret(fixture.secretName));

    await page.goto("/console/regulus/capabilities/", { waitUntil: "networkidle" });
    await expect(page.locator('[data-evidence-id="regulus.capabilities.scope"]')).toContainText(fixture.tenant);
    await expect(page.locator('[data-evidence-id="regulus.capabilities.scope"]')).toContainText(fixture.role);
    if (fixture.canRead) {
      await expect(page.locator('[data-evidence-id="regulus.capabilities.registry"]')).toBeVisible();
      await expect(page.locator('[data-evidence-id^="regulus.capabilities.capability."]')).toHaveCount(
        fixture.expectedCapabilities,
      );
    } else {
      await expect(page.locator('[data-evidence-id="regulus.capabilities.access.restricted"]')).toBeVisible();
      expect(protectedRequests).not.toContain("/v1/econ/regulus/registry/capabilities");
    }
    await testInfo.attach(`${fixture.tenant}-${fixture.role}-capabilities-live-${testInfo.project.name}`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.goto("/console/regulus/enforcement/", { waitUntil: "networkidle" });
    const scope = page.locator('[data-evidence-id="regulus.enforcement.scope"]');
    await expect(scope).toContainText(fixture.tenant);
    await expect(scope).toContainText(fixture.role);
    await expect(scope).toHaveAttribute("data-decision-access", fixture.canMutate ? "enabled" : "read-only");
    if (fixture.canRead) {
      await expect(page.locator('[data-evidence-id="regulus.enforcement.access.restricted"]')).toHaveCount(0);
      await expect(page.locator('[data-evidence-id^="regulus.enforcement.action."]')).toHaveCount(
        fixture.expectedActions,
      );
      if (!fixture.canMutate) {
        await expect(page.locator('[data-evidence-id$=".approve"]')).toHaveCount(0);
        await expect(page.locator('[data-evidence-id$=".reject"]')).toHaveCount(0);
      }
    } else {
      await expect(page.locator('[data-evidence-id="regulus.enforcement.access.restricted"]')).toBeVisible();
      expect(protectedRequests).not.toContain("/v1/econ/regulus/enforcement/actions");
    }
    await assertAccessibility(page, testInfo);
    await testInfo.attach(`${fixture.tenant}-${fixture.role}-enforcement-live-${testInfo.project.name}`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    browserEvidence.assertNoFailedApiResponses();
    await browserEvidence.attach(testInfo);
    await attachSafeJson(testInfo, "live-governance-authorization-result", {
      tenant_id: fixture.tenant,
      role: fixture.role,
      capabilities_read_allowed: fixture.canRead,
      enforcement_decision_allowed: fixture.canMutate,
      actual_capability_rows: fixture.expectedCapabilities,
      actual_enforcement_rows: fixture.expectedActions,
      protected_reads_issued: protectedRequests,
    });
  });
}
