import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import {
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const fixtures = [
  { tenant: "evaluation-studio-v1", apiBase: "http://governance-a.invalid", marker: "Tenant A only", role: "operator", read: false, mutate: false },
  { tenant: "evaluation-studio-v1", apiBase: "http://governance-a.invalid", marker: "Tenant A only", role: "reviewer", read: false, mutate: false },
  { tenant: "evaluation-studio-v1", apiBase: "http://governance-a.invalid", marker: "Tenant A only", role: "admin", read: true, mutate: false },
  { tenant: "evaluation-studio-v1", apiBase: "http://governance-a.invalid", marker: "Tenant A only", role: "platform_admin", read: true, mutate: true },
  { tenant: "evaluation-studio-v1-twin", apiBase: "http://governance-b.invalid", marker: "Tenant B only", role: "operator", read: false, mutate: false },
  { tenant: "evaluation-studio-v1-twin", apiBase: "http://governance-b.invalid", marker: "Tenant B only", role: "reviewer", read: false, mutate: false },
  { tenant: "evaluation-studio-v1-twin", apiBase: "http://governance-b.invalid", marker: "Tenant B only", role: "admin", read: true, mutate: false },
  { tenant: "evaluation-studio-v1-twin", apiBase: "http://governance-b.invalid", marker: "Tenant B only", role: "platform_admin", read: true, mutate: true },
] as const;

async function installDeterministicApi(page: Page, fixture: typeof fixtures[number]) {
  await page.route(`${fixture.apiBase}/**`, async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (pathname === "/health") {
      await json({ status: "ok", deployment_ref: "governance-fixture", graph_version_ref: "1" });
      return;
    }
    if (pathname === "/v1/identity") {
      await json({ subject: `${fixture.role}-fixture`, roles: [fixture.role], tenant_id: fixture.tenant, workspace_id: null });
      return;
    }
    if (pathname === "/v1/deployments" || pathname.endsWith("/approvals")) {
      await json([]);
      return;
    }
    if (pathname === "/v1/econ/regulus/dashboard/kpis") {
      await json({});
      return;
    }
    if (pathname === "/v1/econ/regulus/registry/capabilities") {
      await json([{
        id: "colliding-capability",
        tenant_id: fixture.tenant,
        name: fixture.marker,
        type: "COST",
        description: "Tenant-isolated deterministic fixture",
        criticality: "LOW",
        is_protected: false,
        valuation_config: {},
        created_at: "2026-08-25T12:00:00Z",
      }]);
      return;
    }
    if (pathname === "/v1/econ/regulus/enforcement/actions") {
      await json([{
        id: fixture.tenant.endsWith("twin") ? 22 : 12,
        capability_id: "colliding-capability",
        action_type: "pin_model",
        status: "pending",
        reason: fixture.marker,
        before_config: {},
        after_config: { model: "deterministic-candidate" },
        created_at: "2026-08-25T12:00:00Z",
        approved_at: null,
        approver_sub: null,
      }]);
      return;
    }
    if (pathname === "/v1/econ/regulus/enforcement/policy-actions") {
      await json([]);
      return;
    }
    await json({ detail: "fixture route not cataloged" }, 404);
  });
}

for (const fixture of fixtures) {
  test(`${fixture.tenant} ${fixture.role} has isolated Capabilities and Enforcement states`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    test.skip(!liveEnabled, "requires the isolated local evaluation services");
    test.skip(
      !["desktop-1440", "webkit-1440"].includes(testInfo.project.name),
      "governance acceptance runs in Chromium and WebKit",
    );
    coverCriteria(
      testInfo,
      "regulus.capabilities.authorization",
      "regulus.enforcement.authorization",
      "regulus.tenant-isolation",
      `identity.role.${fixture.role}`,
    );

    await installDeterministicApi(page, fixture);
    const apiOrigin = new URL(fixture.apiBase).origin;
    const browserEvidence = new BrowserEvidence(page, apiOrigin);
    await configurePage(page, fixture.apiBase, fixture.tenant, "deterministic-ui-credential");

    await page.goto("/console/regulus/capabilities/", { waitUntil: "networkidle" });
    const capabilitiesScope = page.locator('[data-evidence-id="regulus.capabilities.scope"]');
    await expect(capabilitiesScope).toContainText(fixture.tenant);
    await expect(capabilitiesScope).toContainText(fixture.role);
    if (fixture.read) {
      await expect(page.locator('[data-evidence-id="regulus.capabilities.registry"]')).toBeVisible();
      await expect(page.locator('[data-evidence-id="regulus.capabilities.access.restricted"]')).toHaveCount(0);
      await expect(page.locator('[data-evidence-id="regulus.capabilities.capability.colliding-capability"]')).toContainText(fixture.marker);
      await expect(page.locator('[data-evidence-id="regulus.capabilities.registry"]')).not.toContainText(
        fixture.tenant.endsWith("twin") ? "Tenant A only" : "Tenant B only",
      );
    } else {
      await expect(page.locator('[data-evidence-id="regulus.capabilities.access.restricted"]')).toBeVisible();
      await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
    }
    await testInfo.attach(`${fixture.tenant}-${fixture.role}-capabilities-${testInfo.project.name}`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    // Reassert the non-secret deterministic connection between route changes.
    // This makes the UI checkpoint independent of dev-server HMR reloads that
    // can replace the document while a long WebKit matrix is in flight.
    await page.evaluate(({ apiBase, tenant }) => {
      window.localStorage.setItem("zeroth.apiBase", apiBase);
      window.localStorage.setItem("zeroth.apiKey", "deterministic-ui-credential");
      window.localStorage.setItem("zeroth.env", "local-evaluation");
      window.localStorage.setItem("zeroth.tenant", tenant);
    }, { apiBase: fixture.apiBase, tenant: fixture.tenant });
    await page.goto("/console/regulus/enforcement/", { waitUntil: "networkidle" });
    const enforcementScope = page.locator('[data-evidence-id="regulus.enforcement.scope"]');
    await expect(enforcementScope).toContainText(fixture.role);
    await expect(enforcementScope).toHaveAttribute(
      "data-decision-access",
      fixture.mutate ? "enabled" : "read-only",
    );
    if (fixture.read) {
      await expect(page.locator('[data-evidence-id="regulus.enforcement.access.restricted"]')).toHaveCount(0);
      const actionId = fixture.tenant.endsWith("twin") ? 22 : 12;
      await expect(page.locator(`[data-evidence-id="regulus.enforcement.action.${actionId}"]`)).toContainText(fixture.marker);
      if (!fixture.mutate) {
        await expect(page.locator('[data-evidence-id$=".approve"]')).toHaveCount(0);
        await expect(page.locator('[data-evidence-id$=".reject"]')).toHaveCount(0);
      }
    } else {
      await expect(page.locator('[data-evidence-id="regulus.enforcement.access.restricted"]')).toBeVisible();
      await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
    }
    await testInfo.attach(`${fixture.tenant}-${fixture.role}-enforcement-${testInfo.project.name}`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    browserEvidence.assertNoFailedApiResponses();
    await browserEvidence.attach(testInfo);
    await attachSafeJson(testInfo, "governance-authorization-result", {
      tenant_id: fixture.tenant,
      role: fixture.role,
      capabilities_read_allowed: fixture.read,
      enforcement_decision_allowed: fixture.mutate,
      stable_api_matrix_verified_by: "tests/service/test_regulus_proxy.py",
      tenant_fixture_marker: fixture.marker,
    });
  });
}
