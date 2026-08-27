import { expect, test } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const phase = process.env.ZEROTH_TEMPLATE_PHASE ?? "create";
const name = "campaign-template-e2e";

async function openCreate(page: import("@playwright/test").Page) {
  await page.locator('[data-evidence-id="templates-new"]').click();
  return {
    name: page.locator('[data-evidence-id="templates-name"]'),
    version: page.locator('[data-evidence-id="templates-version"]'),
    description: page.locator('[data-evidence-id="templates-description"]'),
    body: page.locator('[data-evidence-id="templates-body"]'),
    create: page.locator('[data-evidence-id="templates-create"]'),
  };
}

test.describe("durable prompt templates", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("create, validate, version, and refresh prompt templates", async ({ page, request }, testInfo) => {
    test.skip(phase !== "create", "create phase only");
    coverCriteria(
      testInfo,
      "templates.fields",
      "templates.validation",
      "templates.versioning",
      "templates.refresh-persistence",
    );
    const headers = { "X-API-Key": apiKey! };
    await page.goto("/console/templates/", { waitUntil: "networkidle" });
    const initial = await request.get(`${apiBase}/v1/templates`, { headers });
    const initialTemplates = (await initial.json() as { templates: { name: string; version: number }[] }).templates;
    for (const template of initialTemplates.filter((item) => item.name === name)) {
      const removed = await request.delete(`${apiBase}/v1/templates/${name}/${template.version}`, { headers });
      expect(removed.status()).toBe(204);
    }
    await page.reload({ waitUntil: "networkidle" });

    let form = await openCreate(page);
    await expect(form.create).toBeEnabled();
    await form.create.click();
    await expect(page.getByText("Enter a template name.", { exact: true })).toBeVisible();
    await expect(page.getByText("Enter a Jinja2 template body.", { exact: true })).toBeVisible();
    await expect(form.name).toBeFocused();
    await form.name.fill(name);
    await form.body.fill("Answer {{ input.question }} using only supplied context.");
    await form.version.fill("0");
    await form.create.click();
    await expect(page.getByText("Version must be a whole number from 1 to 1,000,000.")).toBeVisible();
    await expect(form.version).toBeFocused();

    await form.version.fill("1");
    await form.name.fill("../invalid-name");
    await form.create.click();
    await expect(page.getByText(/Use letters, numbers, dots, underscores, or hyphens/)).toBeVisible();
    await expect(form.name).toBeFocused();

    await form.name.fill("invalid-jinja-e2e");
    await form.body.fill("{{ unclosed");
    await form.create.click();
    await expect(page.getByText(/Template body is invalid Jinja2/i)).toBeVisible();
    await expect(form.body).toBeFocused();

    await form.name.fill(name);
    await form.description.fill("Persistent grounded answer template");
    await form.body.fill("Answer {{ input.question }} using only {{ input.context }}.");
    await form.create.click();
    await expect(page.getByText(`${name}@v1`, { exact: true })).toBeVisible();
    await expect(page.getByText("Persistent grounded answer template", { exact: true })).toBeVisible();
    await expect(page.getByText("{{ input }}", { exact: true })).toBeVisible();

    form = await openCreate(page);
    await form.name.fill(name);
    await form.version.fill("2");
    await form.description.fill("Second immutable prompt version");
    await form.body.fill("Revised answer for {{ input.question }}.");
    await form.create.click();
    await expect(page.getByText(`${name}@v2`, { exact: true })).toBeVisible();

    form = await openCreate(page);
    await form.name.fill(name);
    await form.version.fill("2");
    await form.body.fill("Duplicate version");
    await form.create.click();
    await expect(page.getByText(/template name and version already exists/i)).toBeVisible();
    await expect(form.name).toBeFocused();

    await page.reload({ waitUntil: "networkidle" });
    const rows = page.locator('[data-evidence-id^="templates-version-row."]', { hasText: name });
    await expect(rows).toHaveCount(2);
    await rows.filter({ hasText: "v1" }).click();
    await expect(page.getByText("Persistent grounded answer template", { exact: true })).toBeVisible();
    await testInfo.attach("templates-refresh-restored-two-versions", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await assertAccessibility(page, testInfo);
    await attachSafeJson(testInfo, "template-create-result", {
      template_name: name,
      versions: [1, 2],
      invalid_name_rejected: true,
      invalid_jinja_rejected: true,
      duplicate_version_rejected: true,
      description_visible: true,
    });
  });

  test("restore versions after backend restart, then delete disposable fixtures", async ({
    page,
    request,
  }, testInfo) => {
    test.skip(phase !== "restore", "restore phase only");
    coverCriteria(testInfo, "templates.backend-restart-persistence", "templates.delete-version");
    const headers = { "X-API-Key": apiKey! };
    await page.goto("/console/templates/", { waitUntil: "networkidle" });
    let rows = page.locator('[data-evidence-id^="templates-version-row."]', { hasText: name });
    await expect(rows).toHaveCount(2);
    await rows.filter({ hasText: "v1" }).click();
    await expect(page.getByText("Persistent grounded answer template", { exact: true })).toBeVisible();
    await testInfo.attach("templates-restored-after-backend-restart", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    page.on("dialog", (dialog) => dialog.accept());
    await page.locator('[data-evidence-id="templates-delete-version"]').click();
    await expect(rows).toHaveCount(1);
    rows = page.locator('[data-evidence-id^="templates-version-row."]', { hasText: name });
    await rows.click();
    await page.locator('[data-evidence-id="templates-delete-version"]').click();
    await expect(page.locator('[data-evidence-id^="templates-version-row."]', { hasText: name })).toHaveCount(0);
    const after = await request.get(`${apiBase}/v1/templates`, { headers });
    const templates = (await after.json() as { templates: { name: string }[] }).templates;
    expect(templates.some((template) => template.name === name)).toBe(false);
    await testInfo.attach("templates-disposable-versions-removed", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "template-restart-result", {
      template_name: name,
      versions_restored_after_backend_restart: [1, 2],
      disposable_versions_deleted: true,
    });
  });
});
