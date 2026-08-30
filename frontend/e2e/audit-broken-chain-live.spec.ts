import { expect, test } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const enabled = process.env.ZEROTH_EVALUATION_AUDIT_BROKEN_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8123";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1-twin";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const failedAuditId = process.env.ZEROTH_EVALUATION_AUDIT_BROKEN_ID;

test.describe("disposable broken audit-chain product surface", () => {
  test.skip(!enabled, "requires a deliberately tampered disposable audit fixture");

  test("rejects the chain and names the failed record", async ({ page, request }, testInfo) => {
    test.setTimeout(60_000);
    coverCriteria(testInfo, "audit.current-product-surface-broken-chain-rejection");
    expect(apiKey, "disposable tenant admin key is required").toBeTruthy();
    expect(failedAuditId, "expected failed audit identity is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
    const evidence = new BrowserEvidence(page, apiOrigin);

    const identity = await request.get(`${apiBase}/v1/identity`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(identity.status()).toBe(200);
    const identityBody = await identity.json() as { tenant_id?: string };
    expect(identityBody.tenant_id).toBe(tenant);

    const documentResponse = await page.goto("/console/audit/", { waitUntil: "domcontentloaded" });
    expect(documentResponse?.status()).toBe(200);
    await expect(page.locator("main")).toBeVisible();
    const verify = page.locator('[data-evidence-id="audit.verify-chain"]');
    if (!await verify.isVisible({ timeout: 10_000 }).catch(() => false)) {
      // A cold Next development compile can hydrate the prerendered disconnected
      // state before the client observes persisted non-secret session metadata.
      const sessionActive = await page.evaluate(() =>
        window.localStorage.getItem("zeroth.sessionActive"));
      expect(sessionActive).toBe("1");
      await page.reload({ waitUntil: "domcontentloaded" });
    }
    await expect(verify).toBeVisible({ timeout: 20_000 });
    await verify.click();

    const result = page.locator('[data-evidence-id="audit.verify-chain.result"]');
    await expect(result).toHaveAttribute("data-tone", "danger", { timeout: 20_000 });
    await expect(result).toContainText(`chain broken at ${failedAuditId}`);
    await expect(result).toContainText("record digest mismatch");
    await expect(page.getByText("Signing configured", { exact: true })).toBeVisible();
    await assertAccessibility(page, testInfo);
    evidence.assertNoFailedApiResponses();
    await evidence.attach(testInfo);
    await attachSafeJson(testInfo, "broken-chain-result", {
      tenant_id: tenant,
      failed_audit_id: failedAuditId,
      expected_result: "rejected",
      cleanup_state: "fixture-restored-by-coordinator",
    });
    await page.screenshot({
      path: testInfo.outputPath("audit-broken-chain-rejected.png"),
      animations: "disabled",
      fullPage: true,
    });
  });
});
