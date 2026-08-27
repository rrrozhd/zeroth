import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;

test.describe("provider-independent field equivalence classes", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("artifact ID enforces required, missing, and path-security states", async ({ page }, testInfo) => {
    coverCriteria(testInfo, "fields.artifact-id", "artifacts.failure");
    await page.goto("/console/artifacts/", { waitUntil: "networkidle" });
    const input = page.getByRole("textbox", { name: /Artifact ID/ });
    const load = page.getByRole("button", { name: "Load artifact" });

    await load.click();
    await expect(page.getByText("Enter an artifact ID before loading it.")).toBeVisible();
    await testInfo.attach("artifact-required-empty", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await input.fill("../../etc/passwd");
    await load.click();
    await expect(page.getByText(/404|not found/i).first()).toBeVisible();
    await testInfo.attach("artifact-path-security-boundary", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "artifact-field-result", {
      required_empty_rejected: true,
      encoded_path_traversal_not_found: true,
    });
  });

  test("webhook fields reject empty, internal, and unknown-event values without persistence", async ({ page, request }, testInfo) => {
    coverCriteria(testInfo, "fields.webhook-target", "fields.webhook-events", "webhooks.failure");
    const headers = { "X-API-Key": apiKey! };
    const before = await (await request.get(`${apiBase}/v1/webhooks/subscriptions`, { headers })).json();
    await page.goto("/console/webhooks/", { waitUntil: "networkidle" });
    const target = page.getByPlaceholder("https://example.com/hooks/zeroth");
    const completed = page.locator('[data-evidence-id="webhooks.event.run.completed"]');
    const create = page.getByRole("button", { name: "Create subscription" });

    await create.click();
    await expect(page.getByText("Target URL and at least one event type are required.")).toBeVisible();
    await target.fill("http://127.0.0.1:9999/hook");
    await completed.check();
    await create.click();
    await expect(page.getByText(/400|unsafe|internal/i).first()).toBeVisible();
    await testInfo.attach("webhook-internal-destination-rejected", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const invalidEvent = await request.post(`${apiBase}/v1/webhooks/subscriptions`, {
      headers,
      data: {
        deployment_ref: "ignored-by-server",
        tenant_id: tenant,
        target_url: "https://example.com/hooks/zeroth",
        event_types: ["not.an.event"],
      },
    });
    expect(invalidEvent.status()).toBe(422);
    const after = await (await request.get(`${apiBase}/v1/webhooks/subscriptions`, { headers })).json();
    expect(after.total).toBe(before.total);
    await attachSafeJson(testInfo, "webhook-field-result", {
      required_empty_rejected: true,
      internal_destination_rejected: true,
      unknown_event_rejected: true,
      subscription_count_unchanged: true,
    });
  });

  test("static Rightsizing validates bounds and executes both documented endpoints", async ({ page }, testInfo) => {
    coverCriteria(testInfo, "fields.rightsizing-static", "economics-and-rightsizing.boundary");
    await page.goto("/console/rightsizing/", { waitUntil: "networkidle" });
    const incumbent = page.getByRole("textbox", { name: /incumbent The model you run/ });
    const savings = page.getByRole("textbox", { name: /min_savings_%/ });
    const limit = page.getByRole("textbox", { name: /limit Optional cap/ });
    const submit = page.getByRole("button", { name: "Find cheaper models" });
    await expect(submit).toBeDisabled();

    await incumbent.fill("gpt-4o");
    await savings.fill("-0.1");
    await submit.click();
    await expect(page.getByText("Minimum savings must be a number from 0 through 100.")).toBeVisible();
    await savings.fill("0");
    await limit.fill("1");
    const minimumResponse = page.waitForResponse((response) => (
      response.url().endsWith("/v1/econ/rightsizing") && response.request().method() === "POST"
    ));
    await submit.click();
    const minimumResult = await (await minimumResponse).json();
    expect(minimumResult.candidates).toHaveLength(1);
    await expect(page.getByText(minimumResult.candidates[0].model, { exact: true }).first()).toBeVisible();

    await savings.fill("100");
    await limit.fill("20");
    await submit.click();
    await expect(page.getByText(/No alternatives|candidate|savings/i).first()).toBeVisible();
    await testInfo.attach("rightsizing-static-boundaries", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "rightsizing-field-result", {
      required_empty_disabled: true,
      invalid_below_minimum_rejected: true,
      minimums_executed: { min_savings_pct: 0, limit: 1 },
      maximums_executed: { min_savings_pct: 100, limit: 20 },
      provider_calls_made: 0,
    });
  });

  test("campaign budget cap rejects unsafe values and persists a reversible valid edit", async ({ page, request }, testInfo) => {
    coverCriteria(testInfo, "fields.budget-cap", "economics-and-rightsizing.persistence");
    const headers = { "X-API-Key": apiKey! };
    const initial = await (await request.get(`${apiBase}/v1/tenants/${tenant}/cost`, { headers })).json();
    expect(initial.budget_cap_usd).not.toBeNull();
    expect(initial.budget_cap_usd).toBeLessThanOrEqual(10);
    await page.goto("/console/cost/", { waitUntil: "networkidle" });
    const input = page.getByRole("textbox", { name: "Budget cap in USD" });
    const set = page.getByRole("button", { name: "Set", exact: true });

    for (const invalid of ["", "0", "-1", "not-a-number"]) {
      await input.fill(invalid);
      await set.click();
      await expect(page.getByText("Enter a positive USD budget cap.").last()).toBeVisible();
    }
    await input.fill("10.01");
    await set.click();
    await expect(page.getByText(/active campaign ceiling/i)).toBeVisible();

    const reversible = initial.budget_cap_usd === 9.5 ? 9.25 : 9.5;
    await input.fill(String(reversible));
    await set.click();
    await expect(input).toHaveValue(String(reversible));
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByRole("textbox", { name: "Budget cap in USD" })).toHaveValue(String(reversible));
    await testInfo.attach("budget-refresh-persistence", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const restoredInput = page.getByRole("textbox", { name: "Budget cap in USD" });
    await restoredInput.fill(String(initial.budget_cap_usd));
    await page.getByRole("button", { name: "Set", exact: true }).click();
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByRole("textbox", { name: "Budget cap in USD" })).toHaveValue(String(initial.budget_cap_usd));
    await attachSafeJson(testInfo, "budget-field-result", {
      invalid_equivalence_classes_rejected: 5,
      persisted_value: reversible,
      restored_value: initial.budget_cap_usd,
    });
  });
});
