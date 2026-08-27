import { createHash } from "node:crypto";

import { expect, test, type Page, type TestInfo } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const runId = process.env.ZEROTH_EVALUATION_RUN_ID;
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const expectedDeployment = "provider-free-child-approval-d012-20260826-2-parent";
const expectedGraphVersion = "0179d403-2863-45f3-9556-58052a992da8@1";

type Hold = {
  hold_id: string;
  tenant_id: string;
  run_id: string | null;
  reason: string | null;
  active: boolean;
};

type Chain = {
  verified: boolean;
  signature_verified: boolean | null;
  unsigned_record_count: number;
  record_count: number;
};

type History = {
  log_id: string;
  run_id: string | null;
  action: string;
  reason: string | null;
  created_at: string;
};

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, stable(child)]),
    );
  }
  return value;
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(stable(value))).digest("hex");
}

async function shot(testInfo: TestInfo, page: Page, name: string) {
  await testInfo.attach(name, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
}

test("a held run is refused without changing payload or signed evidence", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the persistent local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  expect(runId, "ZEROTH_EVALUATION_RUN_ID is required").toBeTruthy();
  test.setTimeout(90_000);
  coverCriteria(
    testInfo,
    "retention-and-erasure.held-erasure-refusal",
    "retention-and-erasure.persistence",
  );
  await configurePage(page, apiBase, tenant, apiKey!);
  const headers = { "X-API-Key": apiKey! };
  const apiOrigin = new URL(apiBase).origin;
  const network: Array<{ method: string; path: string; status: number }> = [];
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.origin === apiOrigin) {
      network.push({
        method: response.request().method(),
        path: url.pathname,
        status: response.status(),
      });
    }
  });

  const healthResponse = await request.get(`${apiBase}/health`);
  expect(healthResponse.status()).toBe(200);
  const health = await healthResponse.json() as Record<string, unknown>;
  expect(health).toMatchObject({
    status: "ok",
    deployment_ref: expectedDeployment,
    graph_version_ref: expectedGraphVersion,
  });
  const baselineHoldsResponse = await request.get(`${apiBase}/v1/retention/legal-holds`, { headers });
  expect(baselineHoldsResponse.status()).toBe(200);
  const baselineHolds = await baselineHoldsResponse.json() as Hold[];
  expect(baselineHolds.some((hold) => hold.run_id === runId && hold.active)).toBe(false);

  const beforeRunResponse = await request.get(`${apiBase}/v1/runs/${runId}`, { headers });
  const beforeEvidenceResponse = await request.get(`${apiBase}/v1/runs/${runId}/evidence`, { headers });
  expect(beforeRunResponse.status()).toBe(200);
  expect(beforeEvidenceResponse.status()).toBe(200);
  const beforeRun = await beforeRunResponse.json() as Record<string, unknown>;
  const beforeEvidence = await beforeEvidenceResponse.json() as Record<string, unknown>;
  expect(beforeRun).toMatchObject({
    run_id: runId,
    tenant_id: tenant,
    deployment_ref: expectedDeployment,
    graph_version_ref: expectedGraphVersion,
  });
  const beforeChainResponse = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
    headers,
    data: {},
  });
  expect(beforeChainResponse.status()).toBe(200);
  const beforeChain = await beforeChainResponse.json() as Chain;
  expect(beforeChain).toMatchObject({ verified: true, signature_verified: true, unsigned_record_count: 0 });
  expect(beforeChain.record_count).toBeGreaterThan(0);

  let holdId: string | null = null;
  try {
    await page.goto("/console/retention/", { waitUntil: "networkidle" });
    await expect(page.getByText(`tenant ${tenant}`, { exact: false }).first()).toBeVisible();
    await page.locator('[data-evidence-id="retention.legal-holds.run-id"]').fill(runId!);
    await page.locator('[data-evidence-id="retention.legal-holds.reason"]').fill(
      "[VALIDATION] protected erasure refusal; reversible current-build checkpoint",
    );
    await shot(testInfo, page, "retention-hold-configured");

    const holdResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/v1/retention/legal-holds") && response.request().method() === "POST",
    );
    await page.locator('[data-evidence-id="retention.legal-holds.place"]').click();
    const holdResponse = await holdResponsePromise;
    expect(holdResponse.status()).toBe(201);
    const hold = await holdResponse.json() as Hold;
    expect(hold).toMatchObject({ run_id: runId, tenant_id: tenant, active: true });
    holdId = hold.hold_id;

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText(`run ${runId}`, { exact: true })).toBeVisible();
    await shot(testInfo, page, "retention-hold-refresh-restored");

    await page.locator('[data-evidence-id="retention.erasure.scope.run"]').click();
    await page.locator('[data-evidence-id="retention.erasure.run-id"]').fill(runId!);
    await page.locator('[data-evidence-id="retention.erasure.note"]').fill(
      "[VALIDATION] local-only memo; expect 409",
    );
    await page.locator('[data-evidence-id="retention.erasure.stage"]').click();
    await shot(testInfo, page, "retention-erasure-staged");

    page.once("dialog", (dialog) => dialog.accept());
    const refusalPromise = page.waitForResponse((response) =>
      response.url().endsWith("/v1/retention/erasure-requests")
      && response.request().method() === "POST",
    );
    await page.locator('[data-evidence-id="retention.erasure.execute.ER-1"]').click();
    const refusalResponse = await refusalPromise;
    expect(refusalResponse.status()).toBe(409);
    await expect(page.locator('[data-evidence-id="retention.erasure.item.ER-1"]')).toContainText("FAILED");
    await expect(page.locator('[data-evidence-id="retention.erasure.item.ER-1"]')).toContainText(
      /active legal hold and cannot be erased/i,
    );
    await shot(testInfo, page, "retention-held-erasure-refused");

    const afterRunResponse = await request.get(`${apiBase}/v1/runs/${runId}`, { headers });
    const afterEvidenceResponse = await request.get(`${apiBase}/v1/runs/${runId}/evidence`, { headers });
    expect(afterRunResponse.status()).toBe(200);
    expect(afterEvidenceResponse.status()).toBe(200);
    const afterRun = await afterRunResponse.json() as Record<string, unknown>;
    const afterEvidence = await afterEvidenceResponse.json() as Record<string, unknown>;
    expect(digest(afterRun)).toBe(digest(beforeRun));
    expect(digest(afterEvidence)).toBe(digest(beforeEvidence));
    const afterChainResponse = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
      headers,
      data: {},
    });
    expect(afterChainResponse.status()).toBe(200);
    const afterChain = await afterChainResponse.json() as Chain;
    expect(afterChain).toEqual(beforeChain);

    const historyResponse = await request.get(`${apiBase}/v1/retention/erasure-history?limit=50`, {
      headers,
    });
    expect(historyResponse.status()).toBe(200);
    const history = await historyResponse.json() as History[];
    const refusal = history.find((entry) =>
      entry.run_id === runId && entry.action === "erasure_refused_legal_hold" && entry.reason === "rte",
    );
    expect(refusal).toBeTruthy();
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator('[data-evidence-id="retention.erasure.history"]')).toBeVisible();
    await expect(page.getByText(new RegExp(`erasure_refused_legal_hold.*${runId}`))).toBeVisible();
    await shot(testInfo, page, "retention-refusal-history-restored");

    page.once("dialog", (dialog) => dialog.accept());
    const releaseResponsePromise = page.waitForResponse((response) =>
      response.url().endsWith(`/v1/retention/legal-holds/${holdId}`)
      && response.request().method() === "DELETE",
    );
    await page.locator(`[data-evidence-id="retention.legal-holds.release.${holdId}"]`).click();
    expect((await releaseResponsePromise).status()).toBe(200);
    holdId = null;
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText(`run ${runId}`, { exact: true })).toHaveCount(0);
    await shot(testInfo, page, "retention-hold-released");
    await assertAccessibility(page, testInfo);

    const finalHoldsResponse = await request.get(`${apiBase}/v1/retention/legal-holds`, { headers });
    expect(finalHoldsResponse.status()).toBe(200);
    const finalHolds = await finalHoldsResponse.json() as Hold[];
    expect(finalHolds.map((item) => item.hold_id).sort()).toEqual(
      baselineHolds.map((item) => item.hold_id).sort(),
    );
    await attachSafeJson(testInfo, "retention-held-refusal-result", {
      tenant_id: tenant,
      run_id: runId,
      hold_id: hold.hold_id,
      refusal_log_id: refusal!.log_id,
      refusal_action: refusal!.action,
      refusal_status: refusalResponse.status(),
      run_snapshot_sha256: digest(afterRun),
      evidence_snapshot_sha256: digest(afterEvidence),
      run_snapshot_unchanged: true,
      evidence_snapshot_unchanged: true,
      signed_chain: afterChain,
      hold_refresh_restored: true,
      hold_released: true,
      baseline_hold_ids_preserved: baselineHolds.map((item) => item.hold_id).sort(),
      network,
      provider_calls: 0,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
    });
  } finally {
    if (holdId) {
      const cleanup = await request.delete(`${apiBase}/v1/retention/legal-holds/${holdId}`, { headers });
      expect([200, 404]).toContain(cleanup.status());
    }
  }
});
