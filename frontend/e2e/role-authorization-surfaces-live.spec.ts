import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = "evaluation-studio-v1";
const secretRoot = process.env.ZEROTH_EVALUATION_ROLE_SECRET_ROOT
  ?? "/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets";
const surfaceTimeoutMs = 30_000;

const roles = [
  { role: "operator", file: "tenant-a-operator-key", audit: false, economics: false, retention: false },
  { role: "reviewer", file: "tenant-a-reviewer-key", audit: true, economics: false, retention: false },
  { role: "admin", file: "tenant-a-admin-key", audit: true, economics: true, retention: true },
  { role: "platform_admin", file: "service-api-key", audit: true, economics: true, retention: true },
] as const;

async function waitForAllowedSurface(page: Page, label: string): Promise<void> {
  if (label === "Audit") {
    await expect(page.getByRole("table", { name: /audit records/i })).toBeVisible({ timeout: surfaceTimeoutMs });
    return;
  }
  if (label === "Economics") {
    await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled({ timeout: surfaceTimeoutMs });
    await expect(page.getByText("Loading…", { exact: true })).toHaveCount(0, { timeout: surfaceTimeoutMs });
    return;
  }
  await expect(page.getByRole("checkbox", { name: "Retention enforcement enabled" })).toBeVisible({
    timeout: surfaceTimeoutMs,
  });
}

for (const fixture of roles) {
  test(`${fixture.role} sees meaningful authorization states`, async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    test.skip(!liveEnabled, "requires the isolated local evaluation service");
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    coverCriteria(testInfo, "identity.role-denial", `identity.role.${fixture.role}`);
    const key = readFileSync(path.join(secretRoot, fixture.file), "utf8").trim();
    await configurePage(page, apiBase, tenant, key);

    const checkpoints = [
      { route: "/console/audit/", allowed: fixture.audit, label: "Audit" },
      { route: "/console/cost/", allowed: fixture.economics, label: "Economics" },
      { route: "/console/retention/", allowed: fixture.retention, label: "Retention & Compliance" },
    ];
    for (const checkpoint of checkpoints) {
      await page.goto(checkpoint.route, { waitUntil: "domcontentloaded" });
      await expect(page.getByRole("heading", { name: checkpoint.label, exact: true })).toBeVisible({
        timeout: surfaceTimeoutMs,
      });
      const explanation = page.getByText(/hidden because this API key cannot read/i).first();
      if (checkpoint.allowed) {
        await waitForAllowedSurface(page, checkpoint.label);
        await expect(explanation).toHaveCount(0);
      } else {
        await expect(explanation).toBeVisible({ timeout: surfaceTimeoutMs });
        await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
        if (checkpoint.label === "Retention & Compliance") {
          await expect(page.getByRole("button", { name: "Place hold" })).toHaveCount(0);
          await expect(page.getByRole("button", { name: "Stage erasure request" })).toHaveCount(0);
          await expect(page.getByRole("button", { name: /Release legal hold/ })).toHaveCount(0);
        }
      }
      await testInfo.attach(`${fixture.role}-${checkpoint.label.toLowerCase().replaceAll(/[^a-z]+/g, "-")}`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
    }

    await attachSafeJson(testInfo, "authorization-result", {
      tenant_id: tenant,
      role: fixture.role,
      audit_allowed: fixture.audit,
      economics_allowed: fixture.economics,
      retention_allowed: fixture.retention,
    });
  });
}
