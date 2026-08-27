import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const primaryBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const twinBase = process.env.ZEROTH_EVALUATION_TWIN_API_BASE ?? "http://127.0.0.1:8123";
const secretRoot = process.env.ZEROTH_EVALUATION_ROLE_SECRET_ROOT
  ?? "/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets";
const surfaceTimeoutMs = 20_000;

const fixtures = [
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "operator", file: "tenant-a-operator-key", allowed: false },
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "reviewer", file: "tenant-a-reviewer-key", allowed: false },
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "admin", file: "tenant-a-admin-key", allowed: true },
  { tenant: "evaluation-studio-v1", service: primaryBase, other: twinBase, role: "platform_admin", file: "service-api-key", allowed: true },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "operator", file: "tenant-b-operator-key", allowed: false },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "reviewer", file: "tenant-b-reviewer-key", allowed: false },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "admin", file: "tenant-b-admin-key", allowed: true },
  { tenant: "evaluation-studio-v1-twin", service: twinBase, other: primaryBase, role: "platform_admin", file: "tenant-b-platform-admin-key", allowed: true },
] as const;

function credential(filename: string): string {
  return readFileSync(path.join(secretRoot, filename), "utf8").trim();
}

for (const fixture of fixtures) {
  test(`${fixture.tenant} ${fixture.role} has a scoped Webhooks surface`, async ({ page, request }, testInfo) => {
    test.skip(!liveEnabled, "requires the isolated local evaluation services");
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    test.setTimeout(45_000);
    coverCriteria(
      testInfo,
      "webhooks.role-authorization",
      "webhooks.tenant-isolation",
      `identity.role.${fixture.role}`,
    );
    const key = credential(fixture.file);
    const headers = { "X-API-Key": key };
    const missingSubscription = `role-matrix-${fixture.role}-missing`;
    const missingDeadLetter = `role-matrix-${fixture.role}-missing`;

    await configurePage(page, fixture.service, fixture.tenant, key);
    await page.goto("/console/webhooks/", { waitUntil: "networkidle" });
    await expect(page.locator(".console-topbar-breadcrumb")).toHaveAttribute(
      "aria-label",
      `Scope: ${fixture.tenant} / tenant-wide; roles: ${fixture.role}`,
      { timeout: surfaceTimeoutMs },
    );
    await expect(page.locator('[data-evidence-id="webhooks.scope"]')).toContainText(
      `${fixture.tenant} / tenant-wide`,
    );
    await expect(page.locator('[data-evidence-id="webhooks.scope"]')).toContainText(fixture.role);

    const ownList = await request.get(`${fixture.service}/v1/webhooks/subscriptions`, { headers });
    const crossList = await request.get(`${fixture.other}/v1/webhooks/subscriptions`, { headers });
    const ownGet = await request.get(
      `${fixture.service}/v1/webhooks/subscriptions/${missingSubscription}`,
      { headers },
    );
    const crossGet = await request.get(
      `${fixture.other}/v1/webhooks/subscriptions/${missingSubscription}`,
      { headers },
    );
    const ownDeactivate = await request.delete(
      `${fixture.service}/v1/webhooks/subscriptions/${missingSubscription}`,
      { headers },
    );
    const crossDeactivate = await request.delete(
      `${fixture.other}/v1/webhooks/subscriptions/${missingSubscription}`,
      { headers },
    );
    const ownReplay = await request.post(
      `${fixture.service}/v1/webhooks/dead-letters/${missingDeadLetter}/replay`,
      { headers },
    );
    const crossReplay = await request.post(
      `${fixture.other}/v1/webhooks/dead-letters/${missingDeadLetter}/replay`,
      { headers },
    );

    if (fixture.allowed) {
      await expect(page.locator('[data-evidence-id="webhooks.create"]')).toBeVisible();
      await expect(page.locator('[data-evidence-id="webhooks.access.restricted"]')).toHaveCount(0);
      expect(ownList.status()).toBe(200);
      expect(ownGet.status()).toBe(404);
      expect(ownDeactivate.status()).toBe(404);
      expect(ownReplay.status()).toBe(404);
      expect(crossList.status()).toBe(404);
      expect(crossGet.status()).toBe(404);
      expect(crossDeactivate.status()).toBe(404);
      expect(crossReplay.status()).toBe(404);
    } else {
      await expect(page.locator('[data-evidence-id="webhooks.access.restricted"]')).toContainText(
        "admin or platform admin role",
      );
      await expect(page.locator('[data-evidence-id="webhooks.create"]')).toHaveCount(0);
      await expect(page.locator('[data-evidence-id="webhooks.dead-letters.refresh"]')).toHaveCount(0);
      for (const response of [
        ownList,
        crossList,
        ownGet,
        crossGet,
        ownDeactivate,
        crossDeactivate,
        ownReplay,
        crossReplay,
      ]) {
        expect(response.status()).toBe(403);
      }
    }

    for (const response of [crossList, crossGet, crossDeactivate, crossReplay]) {
      expect(Object.keys(await response.json())).toEqual(["detail"]);
    }
    await testInfo.attach(`webhooks-${fixture.tenant}-${fixture.role}`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "webhook-role-scope-result", {
      tenant_id: fixture.tenant,
      role: fixture.role,
      allowed: fixture.allowed,
      own_list_status: ownList.status(),
      cross_list_status: crossList.status(),
      own_get_missing_status: ownGet.status(),
      cross_get_status: crossGet.status(),
      own_deactivate_missing_status: ownDeactivate.status(),
      cross_deactivate_status: crossDeactivate.status(),
      own_replay_missing_status: ownReplay.status(),
      cross_replay_status: crossReplay.status(),
      mutations_performed: 0,
      provider_cost_usd: 0,
      sensitive_material_persisted_to_evidence: false,
    });
  });
}
