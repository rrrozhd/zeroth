import { expect, test, type Page } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const versionedName = process.env.ZEROTH_TEMPLATE_ATOMIC_NAME
  ?? "template-atomicity-ui-20260826-1";
const disposableName = process.env.ZEROTH_TEMPLATE_DISPOSABLE_NAME
  ?? "template-atomicity-disposable-20260826-1";
const referencedName = process.env.ZEROTH_TEMPLATE_REFERENCE_NAME
  ?? "campaign-template-reference-20260826";
const expectedReference = process.env.ZEROTH_TEMPLATE_REFERENCE_KIND ?? "deployment";

async function openCreate(page: Page) {
  await page.locator('[data-evidence-id="templates-new"]').click();
  return {
    name: page.locator('[data-evidence-id="templates-name"]'),
    version: page.locator('[data-evidence-id="templates-version"]'),
    description: page.locator('[data-evidence-id="templates-description"]'),
    body: page.locator('[data-evidence-id="templates-body"]'),
    create: page.locator('[data-evidence-id="templates-create"]'),
  };
}

test("immutable template versions, validation, protected deletion, and disposable deletion", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(90_000);
  test.skip(!liveEnabled, "requires the persistent provider-free evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "canonical functional evidence is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  coverCriteria(
    testInfo,
    "templates.atomic-ui.create",
    "templates.atomic-ui.validation",
    "templates.atomic-ui.immutable-version",
    "templates.atomic-ui.refresh",
    "templates.atomic-ui.reference-delete-denial",
    "templates.atomic-ui.unreferenced-delete",
  );

  await configurePage(page, apiBase, tenant, apiKey!);
  const evidence = new BrowserEvidence(page, apiOrigin);
  const headers = { "X-API-Key": apiKey! };
  const listing = await request.get(`${apiBase}/v1/templates`, { headers });
  expect(listing.status()).toBe(200);
  const templates = (await listing.json() as {
    templates: Array<{ name: string; version: number }>;
  }).templates;
  for (const fixture of templates.filter((item) =>
    [versionedName, disposableName].includes(item.name)
  )) {
    const removed = await request.delete(
      `${apiBase}/v1/templates/${encodeURIComponent(fixture.name)}/${fixture.version}`,
      { headers },
    );
    expect(removed.status()).toBe(204);
  }

  await page.goto("/console/templates/", { waitUntil: "networkidle" });
  await expect(page.locator('[data-evidence-id="templates-scope"]')).toContainText(
    `${tenant} / tenant-wide`,
  );
  await testInfo.attach("templates-before", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  let form = await openCreate(page);
  await form.name.fill(versionedName);
  await form.version.fill("1");
  await form.description.fill("Provider-free atomicity UI fixture");
  await form.body.fill("{{ input.incident_id }");
  await form.create.click();
  await expect(page.getByText(/Template body is invalid Jinja2/i)).toBeVisible();
  await expect(form.body).toBeFocused();
  await testInfo.attach("templates-invalid-jinja", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await form.body.fill("Incident {{ input.incident_id }} is {{ input.status }}.");
  await form.create.click();
  const detail = page.locator('[data-evidence-id="templates-detail"]');
  await expect(detail).toContainText(`${versionedName}@v1`);
  await expect(page.locator('[data-evidence-id="templates-detail-body"]')).toContainText(
    "Incident {{ input.incident_id }} is {{ input.status }}.",
  );
  await testInfo.attach("templates-created-v1", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  form = await openCreate(page);
  await form.name.fill(versionedName);
  await form.version.fill("2");
  await form.description.fill("Second immutable provider-free version");
  await form.body.fill(
    "Incident {{ input.incident_id }} is {{ input.status | upper }}; owner {{ input.owner }}.",
  );
  await form.create.click();
  await expect(detail).toContainText(`${versionedName}@v2`);
  await testInfo.attach("templates-created-v2", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.reload({ waitUntil: "networkidle" });
  const versionRows = page.locator('[data-evidence-id^="templates-version-row."]', {
    hasText: versionedName,
  });
  await expect(versionRows).toHaveCount(2);
  await versionRows.filter({ hasText: "v1" }).click();
  await expect(page.locator('[data-evidence-id="templates-detail-body"]')).toContainText(
    "Incident {{ input.incident_id }} is {{ input.status }}.",
  );
  await testInfo.attach("templates-refresh-restored", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const referenced = page.locator('[data-evidence-id^="templates-version-row."]', {
    hasText: referencedName,
  });
  await expect(referenced).toHaveCount(1);
  await referenced.click();
  page.on("dialog", (dialog) => dialog.accept());
  await page.locator('[data-evidence-id="templates-delete-version"]').click();
  const conflict = page.locator('[data-evidence-id="templates-delete-conflict"]');
  await expect(conflict).toContainText(`${referencedName}@v1 is still in use`);
  await expect(conflict).toContainText(expectedReference);
  await testInfo.attach("templates-referenced-delete-denied", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  form = await openCreate(page);
  await form.name.fill(disposableName);
  await form.version.fill("1");
  await form.body.fill("Disposable {{ input.value }}.");
  await form.create.click();
  await expect(detail).toContainText(`${disposableName}@v1`);
  await testInfo.attach("templates-disposable-before-delete", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.locator('[data-evidence-id="templates-delete-version"]').click();
  await expect(page.locator('[data-evidence-id^="templates-version-row."]', {
    hasText: disposableName,
  })).toHaveCount(0);
  const deleted = await request.get(
    `${apiBase}/v1/templates/${encodeURIComponent(disposableName)}?version=1`,
    { headers },
  );
  expect(deleted.status()).toBe(404);
  await testInfo.attach("templates-disposable-deleted", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await assertAccessibility(page, testInfo);
  await evidence.attach(testInfo);
  await attachSafeJson(testInfo, "template-atomicity-ui-result", {
    tenant_id: tenant,
    workspace_id: null,
    versioned_template_name: versionedName,
    versions: [1, 2],
    invalid_jinja_rejected: true,
    refresh_restored: true,
    referenced_template_name: referencedName,
    reference_kind: expectedReference,
    referenced_delete_status: 409,
    disposable_template_name: disposableName,
    disposable_delete_status: 204,
    provider_calls: 0,
  });
});
