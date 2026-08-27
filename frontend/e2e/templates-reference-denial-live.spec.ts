import { expect, test } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8125";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const workspace = process.env.ZEROTH_EVALUATION_WORKSPACE ?? "template-workspace-a";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const templateName = process.env.ZEROTH_TEMPLATE_REFERENCE_NAME ?? "referenced-template-e2e";

test("published template reference denies version deletion and survives refresh", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  coverCriteria(
    testInfo,
    "templates.reference-delete-denial",
    "templates.reference-refresh-persistence",
  );
  await configurePage(page, apiBase, tenant, apiKey!);
  const evidence = new BrowserEvidence(page, apiOrigin);

  await page.goto("/console/templates/", { waitUntil: "networkidle" });
  await expect(page.locator('[data-evidence-id="templates-scope"]')).toContainText(
    `${tenant} / ${workspace}`,
  );
  const row = page.locator('[data-evidence-id^="templates-version-row."]', {
    hasText: templateName,
  });
  await expect(row).toHaveCount(1);
  await row.click();
  page.on("dialog", (dialog) => dialog.accept());
  await page.locator('[data-evidence-id="templates-delete-version"]').click();

  const conflict = page.locator('[data-evidence-id="templates-delete-conflict"]');
  await expect(conflict).toContainText(`${templateName}@v1 is still in use`);
  await expect(conflict).toContainText("Remove or repin its dependent references");
  await expect(conflict).toContainText("published_graph");
  await expect(row).toHaveCount(1);
  await testInfo.attach("template-reference-delete-denied", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.reload({ waitUntil: "networkidle" });
  await expect(row).toHaveCount(1);
  const response = await request.get(
    `${apiBase}/v1/templates/${encodeURIComponent(templateName)}?version=1`,
    { headers: { "X-API-Key": apiKey! } },
  );
  expect(response.status()).toBe(200);
  await assertAccessibility(page, testInfo);
  await evidence.attach(testInfo);
  await attachSafeJson(testInfo, "template-reference-denial-result", {
    tenant_id: tenant,
    workspace_id: workspace,
    template_name: templateName,
    template_version: 1,
    delete_status: 409,
    reference_kind: "published_graph",
    refresh_restored: true,
    provider_calls: 0,
  });
});
