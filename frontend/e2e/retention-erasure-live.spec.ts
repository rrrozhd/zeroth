import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8123";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1-twin";
const deploymentRef = process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF ?? "demo-artifact-output-v1";
const campaignId = process.env.ZEROTH_EVALUATION_CAMPAIGN_ID ?? "evaluation-studio-v1-twin";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;

type ArtifactRef = { key: string; content_type: string; size: number };
type Run = {
  run_id: string;
  status: string;
  tenant_id: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output?: { artifact?: ArtifactRef } | null;
};
type ErasureRun = {
  run_id: string;
  audits_erased: number;
  checkpoints_deleted: number;
  run_redacted: boolean;
  artifacts_deleted: number;
  econ_events_deleted: number | null;
  cleanup_state: "complete" | "failed" | "pending";
  authorization_log_id: string | null;
  retry_log_id: string | null;
};
type ErasureResponse = { reason: string; runs: ErasureRun[] };

const headers = () => ({ "X-API-Key": apiKey! });

async function createArtifactRun(
  request: APIRequestContext,
  label: string,
): Promise<{ run: Run; artifact: ArtifactRef }> {
  const response = await request.post(`${apiBase}/v1/runs`, {
    headers: headers(),
    data: {
      input_payload: { kind: "json", label },
      campaign_id: campaignId,
      campaign_strict: true,
    },
  });
  expect(response.status()).toBe(202);
  const created = await response.json() as Run;
  let run = created;
  await expect.poll(async () => {
    const current = await request.get(`${apiBase}/v1/runs/${created.run_id}`, { headers: headers() });
    expect(current.status()).toBe(200);
    run = await current.json() as Run;
    return run.status;
  }, { timeout: 30_000, intervals: [200, 400, 800] }).toBe("succeeded");
  expect(run.tenant_id).toBe(tenant);
  expect(run.deployment_ref).toBe(deploymentRef);
  expect(run.terminal_output?.artifact).toBeTruthy();
  return { run, artifact: run.terminal_output!.artifact! };
}

async function stageRunErasure(page: Page, runId: string, note: string) {
  await page.locator('[data-evidence-id="retention.erasure.scope.run"]').click();
  await page.locator('[data-evidence-id="retention.erasure.run-id"]').fill(runId);
  await page.locator('[data-evidence-id="retention.erasure.note"]').fill(note);
  await page.locator('[data-evidence-id="retention.erasure.stage"]').click();
}

async function executeNewest(page: Page) {
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator('[data-evidence-id^="retention.erasure.execute."]').first().click();
}

async function screenshot(testInfo: TestInfo, page: Page, name: string) {
  await testInfo.attach(name, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
}

function safeErasureEvidence(row: ErasureRun) {
  const { authorization_log_id, retry_log_id, ...counts } = row;
  return {
    ...counts,
    cleanup_log_id: authorization_log_id,
    cleanup_retry_log_id: retry_log_id,
  };
}

test("held, direct, and tenant erasure remain chain-safe and economics-aware", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "destructive checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(150_000);
  coverCriteria(
    testInfo,
    "retention-and-erasure.held-erasure-refusal",
    "retention-and-erasure.nonheld-run-erasure",
    "retention-and-erasure.tenant-erasure",
    "retention-and-erasure.audit-chain-after-erasure",
    "retention-and-erasure.economics-after-erasure",
  );
  await configurePage(page, apiBase, tenant, apiKey!);

  const held = await createArtifactRun(request, "retention-held-disposable");
  const direct = await createArtifactRun(request, "retention-direct-disposable");
  const tenantCandidate = await createArtifactRun(request, "retention-tenant-disposable");
  const economicsBeforeResponse = await request.get(
    `${apiBase}/v1/econ/unit-economics?scope=tenant&window=50`,
    { headers: headers() },
  );
  expect(economicsBeforeResponse.status()).toBe(200);
  const economicsBefore = await economicsBeforeResponse.json() as Record<string, unknown>;

  await page.goto("/console/retention/", { waitUntil: "networkidle" });
  await expect(page.getByText(`tenant ${tenant}`, { exact: false })).toBeVisible();
  await page.locator('[data-evidence-id="retention.legal-holds.run-id"]').fill(held.run.run_id);
  await page.locator('[data-evidence-id="retention.legal-holds.reason"]').fill(
    "[VALIDATION] hold must survive run and tenant erasure",
  );
  await screenshot(testInfo, page, "retention-held-run-configured");
  const holdResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/v1/retention/legal-holds") && response.request().method() === "POST",
  );
  await page.locator('[data-evidence-id="retention.legal-holds.place"]').click();
  const holdResponse = await holdResponsePromise;
  expect(holdResponse.status()).toBe(201);
  const hold = await holdResponse.json() as { hold_id: string; run_id: string; tenant_id: string };
  expect(hold).toMatchObject({ run_id: held.run.run_id, tenant_id: tenant });
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.getByText(`run ${held.run.run_id}`, { exact: true })).toBeVisible();

  await stageRunErasure(page, held.run.run_id, "[VALIDATION] expect legal-hold refusal");
  await screenshot(testInfo, page, "retention-held-erasure-staged");
  const refusedResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/v1/retention/erasure-requests") && response.request().method() === "POST",
  );
  await executeNewest(page);
  const refusedResponse = await refusedResponsePromise;
  expect(refusedResponse.status()).toBe(409);
  await expect(page.getByText("FAILED", { exact: true })).toBeVisible();
  await expect(page.getByText(/active legal hold and cannot be erased/i).first()).toBeVisible();
  await screenshot(testInfo, page, "retention-held-erasure-refused");
  const heldArtifactAfterRefusal = await request.get(`${apiBase}/v1/artifacts/${held.artifact.key}`, {
    headers: headers(),
  });
  expect(heldArtifactAfterRefusal.status()).toBe(200);

  await stageRunErasure(page, direct.run.run_id, "[VALIDATION] direct disposable run erasure");
  await screenshot(testInfo, page, "retention-direct-erasure-staged");
  const directResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/v1/retention/erasure-requests") && response.request().method() === "POST",
  );
  await executeNewest(page);
  const directResponse = await directResponsePromise;
  expect(directResponse.status()).toBe(200);
  const directErasure = await directResponse.json() as ErasureResponse;
  const directResult = directErasure.runs.find((row) => row.run_id === direct.run.run_id);
  expect(directResult).toMatchObject({
    run_redacted: true,
    artifacts_deleted: 1,
    cleanup_state: "complete",
  });
  expect(directResult?.authorization_log_id).toBeTruthy();
  expect(directResult?.audits_erased).toBeGreaterThan(0);
  expect(directResult?.econ_events_deleted).not.toBeNull();
  await expect(page.locator('[data-evidence-id="retention.erasure.item.ER-2"]')).toContainText("ERASED");
  await expect(page.locator('[data-evidence-id="retention.erasure.item.ER-2"]')).toContainText("econ");
  await screenshot(testInfo, page, "retention-direct-erasure-completed");

  const directRunAfter = await (
    await request.get(`${apiBase}/v1/runs/${direct.run.run_id}`, { headers: headers() })
  ).json() as Run;
  expect(directRunAfter.terminal_output).toBeNull();
  expect((await request.get(`${apiBase}/v1/artifacts/${direct.artifact.key}`, { headers: headers() })).status()).toBe(404);
  const directChain = await (
    await request.post(`${apiBase}/v1/runs/${direct.run.run_id}/verify-chain`, {
      headers: headers(),
      data: {},
    })
  ).json() as { verified: boolean; signature_verified: boolean | null; record_count: number };
  expect(directChain).toMatchObject({ verified: true, signature_verified: true });
  expect(directChain.record_count).toBeGreaterThan(0);
  const directEvidence = await (
    await request.get(`${apiBase}/v1/runs/${direct.run.run_id}/evidence`, { headers: headers() })
  ).json() as { audits: Array<{ erased: boolean; erasure_reason?: string | null }> };
  expect(directEvidence.audits.length).toBeGreaterThan(0);
  expect(directEvidence.audits.every((record) => record.erased)).toBe(true);

  await page.locator('[data-evidence-id="retention.erasure.scope.tenant"]').click();
  await expect(page.locator('[data-evidence-id="retention.erasure.tenant-id"]')).toHaveValue(tenant);
  await page.locator('[data-evidence-id="retention.erasure.note"]').fill(
    "[VALIDATION] erase every non-held twin-tenant run",
  );
  await page.locator('[data-evidence-id="retention.erasure.stage"]').click();
  await screenshot(testInfo, page, "retention-tenant-erasure-staged");
  const tenantResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith("/v1/retention/erasure-requests") && response.request().method() === "POST",
  );
  await executeNewest(page);
  const tenantResponse = await tenantResponsePromise;
  expect(tenantResponse.status()).toBe(200);
  const tenantErasure = await tenantResponse.json() as ErasureResponse;
  expect(tenantErasure.runs.some((row) => row.run_id === tenantCandidate.run.run_id)).toBe(true);
  expect(tenantErasure.runs.some((row) => row.run_id === held.run.run_id)).toBe(false);
  expect(tenantErasure.runs.every((row) => row.econ_events_deleted !== null)).toBe(true);
  await expect(page.locator('[data-evidence-id="retention.erasure.item.ER-3"]')).toContainText("ERASED");
  await screenshot(testInfo, page, "retention-tenant-erasure-completed");

  const heldRunAfterTenant = await (
    await request.get(`${apiBase}/v1/runs/${held.run.run_id}`, { headers: headers() })
  ).json() as Run;
  expect(heldRunAfterTenant.terminal_output?.artifact?.key).toBe(held.artifact.key);
  expect((await request.get(`${apiBase}/v1/artifacts/${held.artifact.key}`, { headers: headers() })).status()).toBe(200);
  const tenantRunAfter = await (
    await request.get(`${apiBase}/v1/runs/${tenantCandidate.run.run_id}`, { headers: headers() })
  ).json() as Run;
  expect(tenantRunAfter.terminal_output).toBeNull();
  expect((await request.get(`${apiBase}/v1/artifacts/${tenantCandidate.artifact.key}`, { headers: headers() })).status()).toBe(404);
  const tenantChain = await (
    await request.post(`${apiBase}/v1/runs/${tenantCandidate.run.run_id}/verify-chain`, {
      headers: headers(),
      data: {},
    })
  ).json() as { verified: boolean; signature_verified: boolean | null; record_count: number };
  expect(tenantChain).toMatchObject({ verified: true, signature_verified: true });

  const economicsAfterResponse = await request.get(
    `${apiBase}/v1/econ/unit-economics?scope=tenant&window=50`,
    { headers: headers() },
  );
  expect(economicsAfterResponse.status()).toBe(200);
  const economicsAfter = await economicsAfterResponse.json() as Record<string, unknown>;
  for (const report of [economicsBefore, economicsAfter]) {
    expect(Number(report.total_cost_usd)).toBeGreaterThanOrEqual(0);
    expect(Number.isFinite(Number(report.total_cost_usd))).toBe(true);
  }
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.locator('[data-evidence-id="retention.erasure.history"]')).toBeVisible();
  await expect(page.getByText(new RegExp(`erase_run_complete.*${tenantCandidate.run.run_id}`))).toBeVisible();
  await screenshot(testInfo, page, "retention-erasure-history-refresh-restored");
  await assertAccessibility(page, testInfo);
  await attachSafeJson(testInfo, "retention-erasure-result", {
    tenant_id: tenant,
    deployment_ref: deploymentRef,
    graph_version_ref: direct.run.graph_version_ref,
    hold_id: hold.hold_id,
    held_run_id: held.run.run_id,
    direct_run_id: direct.run.run_id,
    tenant_run_id: tenantCandidate.run.run_id,
    refusal_status: refusedResponse.status(),
    direct_erasure: directResult ? safeErasureEvidence(directResult) : null,
    tenant_erasure_runs: tenantErasure.runs.map(safeErasureEvidence),
    direct_chain: directChain,
    tenant_chain: tenantChain,
    held_artifact_survived: true,
    direct_artifact_erased: true,
    tenant_artifact_erased: true,
    economics_before: economicsBefore,
    economics_after: economicsAfter,
    provider_calls: 0,
  });
});
