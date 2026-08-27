import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;

type Policy = {
  tenant_id: string;
  enabled: boolean;
  run_ttl_seconds: number | null;
  audit_ttl_seconds: number | null;
};

type Hold = {
  hold_id: string;
  run_id: string | null;
  reason: string | null;
  active: boolean;
};

test.describe("Retention and Compliance reversible live validation", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("policy boundaries persist through refresh and restore exactly", async ({ page, request }, testInfo) => {
    coverCriteria(
      testInfo,
      "retention-and-erasure.boundary",
      "retention-and-erasure.persistence",
      "fields.retention-policy",
    );
    const headers = { "X-API-Key": apiKey! };
    const original = await (await request.get(`${apiBase}/v1/retention/policy`, { headers })).json() as Policy;

    try {
      await page.goto("/console/retention/", { waitUntil: "networkidle" });
      const enabled = page.getByRole("checkbox", { name: "Retention enforcement enabled" });
      const runTtl = page.getByRole("textbox", { name: "Run payloads TTL in days" });
      const auditTtl = page.getByRole("textbox", { name: "Audit records TTL in days" });
      const save = page.getByRole("button", { name: "Save policy" });

      await runTtl.fill("0");
      await expect(runTtl).toHaveAttribute("aria-invalid", "true");
      await expect(page.getByText(/TTL must resolve to 1–2147483647 seconds/)).toBeVisible();
      await expect(save).toBeDisabled();
      await testInfo.attach("retention-zero-ttl-rejected", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      await runTtl.fill("not-a-number");
      await expect(runTtl).toHaveAttribute("aria-invalid", "true");
      await runTtl.fill("24855.1349");
      await expect(runTtl).toHaveAttribute("aria-invalid", "true");
      await runTtl.fill("");
      await auditTtl.fill("24855.1348");
      if (await enabled.isChecked()) await enabled.uncheck();

      const saveResponse = page.waitForResponse((response) =>
        response.url().endsWith("/v1/retention/policy") && response.request().method() === "PUT",
      );
      await save.click();
      const resolvedSaveResponse = await saveResponse;
      expect(resolvedSaveResponse.status()).toBe(200);
      expect((await resolvedSaveResponse.json() as Policy).audit_ttl_seconds).toBe(2_147_483_647);
      await testInfo.attach("retention-policy-blank-and-maximum-configured", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByRole("checkbox", { name: "Retention enforcement enabled" })).not.toBeChecked();
      await expect(page.getByRole("textbox", { name: "Run payloads TTL in days" })).toHaveValue("");
      await expect(page.getByRole("textbox", { name: "Audit records TTL in days" })).toHaveValue("24855.1348");
      await testInfo.attach("retention-policy-blank-and-maximum-refresh-restored", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      await page.getByRole("textbox", { name: "Run payloads TTL in days" }).fill("24855.1348");
      await page.getByRole("textbox", { name: "Audit records TTL in days" }).fill("");
      const inverseSaveResponse = page.waitForResponse((response) =>
        response.url().endsWith("/v1/retention/policy") && response.request().method() === "PUT",
      );
      await page.getByRole("button", { name: "Save policy" }).click();
      expect((await inverseSaveResponse).status()).toBe(200);
      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByRole("textbox", { name: "Run payloads TTL in days" })).toHaveValue("24855.1348");
      await expect(page.getByRole("textbox", { name: "Audit records TTL in days" })).toHaveValue("");
      await testInfo.attach("retention-policy-inverse-boundaries-refresh-restored", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      await page.getByRole("textbox", { name: "Run payloads TTL in days" }).fill("0.0000115741");
      const oneSecondSaveResponse = page.waitForResponse((response) =>
        response.url().endsWith("/v1/retention/policy") && response.request().method() === "PUT",
      );
      await page.getByRole("button", { name: "Save policy" }).click();
      const resolvedOneSecondResponse = await oneSecondSaveResponse;
      expect(resolvedOneSecondResponse.status()).toBe(200);
      expect((await resolvedOneSecondResponse.json() as Policy).run_ttl_seconds).toBe(1);
      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByRole("textbox", { name: "Run payloads TTL in days" })).toHaveValue("0.0000115741");
      await expect(page.getByText("1s", { exact: true })).toBeVisible();
      await testInfo.attach("retention-policy-one-second-refresh-restored", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
    } finally {
      const restored = await request.put(`${apiBase}/v1/retention/policy`, {
        headers,
        data: {
          enabled: original.enabled,
          run_ttl_seconds: original.run_ttl_seconds,
          audit_ttl_seconds: original.audit_ttl_seconds,
        },
      });
      expect(restored.status()).toBe(200);
    }

    const after = await (await request.get(`${apiBase}/v1/retention/policy`, { headers })).json() as Policy;
    expect(after).toMatchObject(original);
    await attachSafeJson(testInfo, "retention-policy-result", {
      zero_rejected: true,
      non_numeric_rejected: true,
      over_maximum_rejected: true,
      one_second_round_trip: true,
      no_expiry_persisted_in_both_fields: true,
      maximum_seconds_persisted_in_both_fields: 2_147_483_647,
      disabled_state_persisted: true,
      original_policy_restored: true,
    });
  });

  test("run and tenant legal holds persist, release, and preserve baseline", async ({ page, request }, testInfo) => {
    coverCriteria(
      testInfo,
      "retention-and-erasure.held",
      "retention-and-erasure.persistence",
      "fields.legal-hold",
    );
    const headers = { "X-API-Key": apiKey! };
    const baseline = await (await request.get(`${apiBase}/v1/retention/legal-holds`, { headers })).json() as Hold[];
    const created = new Set<string>();
    const runList = await (await request.get(`${apiBase}/v1/admin/runs?limit=50`, { headers })).json() as {
      runs: Array<{ run_id: string }>;
    };
    const heldRuns = new Set(baseline.map((hold) => hold.run_id).filter(Boolean));
    const fixtureRun = runList.runs.find((run) => !heldRuns.has(run.run_id))?.run_id;
    expect(fixtureRun, "a real unheld tenant run is required for reversible hold validation").toBeTruthy();

    try {
      await page.goto("/console/retention/", { waitUntil: "networkidle" });
      const runId = page.getByRole("textbox", { name: "Legal hold run ID" });
      const reason = page.getByRole("textbox", { name: "Legal hold reason" });
      const place = page.getByRole("button", { name: "Place hold" });

      await runId.fill(`missing-run-${Date.now()}`);
      await reason.fill("[VALIDATION] reversible run-scoped hold");
      const missingResponse = page.waitForResponse((response) =>
        response.url().endsWith("/v1/retention/legal-holds") && response.request().method() === "POST",
      );
      await place.click();
      expect((await missingResponse).status()).toBe(404);
      await expect(page.getByText(/run not found/i).last()).toBeVisible();
      await testInfo.attach("legal-hold-missing-run-rejected", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      await runId.fill(fixtureRun!);
      await testInfo.attach("legal-hold-run-configured", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      const runResponse = page.waitForResponse((response) =>
        response.url().endsWith("/v1/retention/legal-holds") && response.request().method() === "POST",
      );
      await place.click();
      const resolvedRunResponse = await runResponse;
      const runHold = await resolvedRunResponse.json() as Hold;
      if (runHold.hold_id) created.add(runHold.hold_id);
      expect(resolvedRunResponse.status()).toBe(201);
      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByText(`run ${fixtureRun}`, { exact: true })).toBeVisible();

      await reason.fill("[VALIDATION] reversible tenant-wide hold");
      const tenantResponse = page.waitForResponse((response) =>
        response.url().endsWith("/v1/retention/legal-holds") && response.request().method() === "POST",
      );
      await place.click();
      const resolvedTenantResponse = await tenantResponse;
      const tenantHold = await resolvedTenantResponse.json() as Hold;
      if (tenantHold.hold_id) created.add(tenantHold.hold_id);
      expect(resolvedTenantResponse.status()).toBe(201);
      await expect(page.getByText("tenant-wide", { exact: true })).toBeVisible();

      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByText(`run ${fixtureRun}`, { exact: true })).toBeVisible();
      await expect(page.getByText("[VALIDATION] reversible tenant-wide hold", { exact: true })).toBeVisible();
      await testInfo.attach("legal-holds-refresh-persistence", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      for (const holdId of [runHold.hold_id, tenantHold.hold_id]) {
        page.once("dialog", (dialog) => dialog.accept());
        const releaseResponse = page.waitForResponse((response) =>
          response.url().endsWith(`/v1/retention/legal-holds/${holdId}`) && response.request().method() === "DELETE",
        );
        await page.getByRole("button", { name: `Release legal hold ${holdId}` }).click();
        expect((await releaseResponse).status()).toBe(200);
        created.delete(holdId);
      }
      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByText(`run ${fixtureRun}`, { exact: true })).toHaveCount(0);
      await testInfo.attach("legal-holds-released", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
    } finally {
      for (const holdId of created) {
        await request.delete(`${apiBase}/v1/retention/legal-holds/${holdId}`, { headers });
      }
    }

    const after = await (await request.get(`${apiBase}/v1/retention/legal-holds`, { headers })).json() as Hold[];
    expect(after.map((hold) => hold.hold_id).sort()).toEqual(baseline.map((hold) => hold.hold_id).sort());
    await attachSafeJson(testInfo, "legal-hold-result", {
      run_scoped_hold_persisted: true,
      tenant_wide_hold_persisted: true,
      both_released: true,
      baseline_hold_ids_preserved: baseline.map((hold) => hold.hold_id),
    });
  });
});
