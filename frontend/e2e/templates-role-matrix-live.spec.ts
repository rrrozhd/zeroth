import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

import {
  assertAccessibility,
  assertMinimumTargets,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const workspace = process.env.ZEROTH_EVALUATION_WORKSPACE?.trim() || null;
const secretRoot = process.env.ZEROTH_EVALUATION_ROLE_SECRET_ROOT
  ?? "/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets";
const secretPrefix = process.env.ZEROTH_EVALUATION_ROLE_SECRET_PREFIX ?? "tenant-a";
const expectedScope = `${tenant} / ${workspace ?? "tenant-wide"}`;

const fixtures = [
  { role: "operator", file: `${secretPrefix}-operator-key`, canMutate: false },
  { role: "reviewer", file: `${secretPrefix}-reviewer-key`, canMutate: false },
  { role: "admin", file: `${secretPrefix}-admin-key`, canMutate: true },
  { role: "platform_admin", file: `${secretPrefix}-platform-admin-key`, canMutate: true },
] as const;

async function keyboardReach(page: Page, evidenceId: string, projectName: string): Promise<void> {
  await page.locator("body").click({ position: { x: 1, y: 1 } });
  const tabKey = process.platform === "darwin" && projectName.startsWith("webkit")
    ? "Alt+Tab"
    : "Tab";
  for (let index = 0; index < 50; index += 1) {
    await page.keyboard.press(tabKey);
    if (await page.evaluate((target) => {
      const active = document.activeElement as HTMLElement | null;
      return active?.dataset.evidenceId === target;
    }, evidenceId)) {
      await expect(page.locator(`[data-evidence-id="${evidenceId}"]`).first()).toBeFocused();
      expect(await page.evaluate(() => document.activeElement?.matches(":focus-visible"))).toBe(true);
      return;
    }
  }
  throw new Error(`keyboard focus did not reach ${evidenceId}`);
}

for (const fixture of fixtures) {
  test(`${fixture.role} sees the tenant-scoped Templates capability state`, async ({ page }, testInfo) => {
    test.setTimeout(90_000);
    test.skip(!liveEnabled, "requires the isolated local evaluation service");
    test.skip(
      !["desktop-1440", "webkit-1440"].includes(testInfo.project.name),
      "role evidence uses canonical Chromium and WebKit viewports",
    );
    coverCriteria(
      testInfo,
      `templates.role.${fixture.role}`,
      "templates.role-matrix",
      "templates.scope",
      "templates.keyboard",
      "templates.refresh-persistence",
      "templates.accessibility",
    );

    const key = readFileSync(path.join(secretRoot, fixture.file), "utf8").trim();
    await configurePage(page, apiBase, tenant, key);
    const evidence = new BrowserEvidence(page, apiOrigin);

    await page.goto("/console/templates/", { waitUntil: "networkidle" });
    const scope = page.locator('[data-evidence-id="templates-scope"]');
    await expect(scope).toContainText(expectedScope, { timeout: 15_000 });
    await expect(scope).toContainText(fixture.role, { timeout: 15_000 });
    const firstTemplateRow = page.locator('[data-evidence-id^="templates-version-row."]').first();
    await expect(firstTemplateRow).toBeVisible();

    const newTemplate = page.locator('[data-evidence-id="templates-new"]');
    if (fixture.canMutate) {
      await expect(scope).toContainText("Create and delete are enabled by template:admin");
      await expect(newTemplate).toBeVisible();
      await keyboardReach(page, "templates-new", testInfo.project.name);
      await testInfo.attach(`${fixture.role}-keyboard-focus`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      await page.keyboard.press("Enter");
      const name = page.locator('[data-evidence-id="templates-name"]');
      await expect(name).toBeVisible();
      await page.locator('[data-evidence-id="templates-create"]').click();
      await expect(page.getByText("Enter a template name.", { exact: true })).toBeVisible();
      await expect(page.getByText("Enter a Jinja2 template body.", { exact: true })).toBeVisible();
      await expect(name).toBeFocused();
    } else {
      await expect(scope).toContainText(`${fixture.role} does not include template:admin`);
      await expect(scope).toContainText("templates are read-only in this scope");
      await expect(newTemplate).toHaveCount(0);
      const firstTemplateRowId = await firstTemplateRow.getAttribute("data-evidence-id");
      expect(firstTemplateRowId).toBeTruthy();
      await keyboardReach(page, firstTemplateRowId!, testInfo.project.name);
      await testInfo.attach(`${fixture.role}-keyboard-focus`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      await page.keyboard.press("Enter");
      await expect(page.locator('[data-evidence-id="templates-delete-version"]')).toHaveCount(0);
    }

    await testInfo.attach(`${fixture.role}-capability-state`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await assertAccessibility(page, testInfo);
    await assertMinimumTargets(page, testInfo);

    await page.reload({ waitUntil: "networkidle" });
    await expect(scope).toContainText(expectedScope, { timeout: 15_000 });
    await expect(scope).toContainText(fixture.role, { timeout: 15_000 });
    if (fixture.canMutate) await expect(newTemplate).toBeVisible();
    else await expect(newTemplate).toHaveCount(0);
    await testInfo.attach(`${fixture.role}-refresh-restored`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    evidence.assertNoFailedApiResponses();
    await evidence.attach(testInfo);
    await attachSafeJson(testInfo, "templates-role-result", {
      tenant_id: tenant,
      workspace_id: workspace,
      role: fixture.role,
      mutation_controls_visible: fixture.canMutate,
      keyboard_reachable: true,
      refresh_restored: true,
      browser: testInfo.project.name,
    });
  });
}
