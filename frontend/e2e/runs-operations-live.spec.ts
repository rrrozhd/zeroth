import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const deploymentRef = process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF
  ?? "demo-data-quality-repair-loop-manifest-v1";
const graphVersionRef = process.env.ZEROTH_EVALUATION_GRAPH_VERSION_REF
  ?? "43dc0a14-e924-4d3e-8763-740408ebee3a@2";

async function createRun(
  request: import("@playwright/test").APIRequestContext,
  campaignId: string,
  inputPayload: Record<string, unknown>,
) {
  const response = await request.post(`${apiBase}/v1/runs`, {
    headers: { "X-API-Key": apiKey! },
    data: { campaign_id: campaignId, input_payload: inputPayload },
  });
  expect(response.status()).toBe(202);
  return (await response.json() as { run_id: string }).run_id;
}

async function waitForStatus(
  request: import("@playwright/test").APIRequestContext,
  runId: string,
  expected: string,
) {
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    return (await response.json() as { status: string }).status;
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe(expected);
}

test("Runs filters, failure detail, signed evidence, safe invoke, and replay operate end to end", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(
    testInfo,
    "runs.filtering",
    "runs.failure-display",
    "runs.evidence",
    "runs.replay",
    "runs.curl-copy",
    "runs.filter-failure-replay",
  );

  await configurePage(page, apiBase, tenant, apiKey!);
  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string;
    graph_version_ref: string;
    campaign_id: string;
  };
  expect(health).toMatchObject({ deployment_ref: deploymentRef, graph_version_ref: graphVersionRef });

  const successfulRunId = await createRun(request, health.campaign_id, {
    records: [{ name: "Grace", email: "grace@example.test", status: "active" }],
  });
  const failedRunId = await createRun(request, health.campaign_id, {
    records: [],
    force_failure: true,
    evaluation_delay_ms: 9_000,
  });
  await waitForStatus(request, successfulRunId, "succeeded");
  await waitForStatus(request, failedRunId, "failed");

  await page.goto(`/console/runs/?run=${successfulRunId}`, { waitUntil: "domcontentloaded" });
  const filter = page.locator('[data-evidence-id="runs.filter.status"]');
  // A static-exported select is present before React hydrates. Wait for a run
  // row loaded by the client before changing it so the controlled value cannot
  // be reset to `all` by late hydration.
  await expect(page.locator(`[data-evidence-id="runs.run.${successfulRunId}"]`)).toBeVisible();
  await filter.selectOption("succeeded");
  await expect(filter).toHaveValue("succeeded");
  await expect(page.locator(`[data-evidence-id="runs.run.${successfulRunId}"]`)).toBeVisible();
  await expect(page.locator(`[data-evidence-id="runs.run.${failedRunId}"]`)).toHaveCount(0);

  await filter.selectOption("failed");
  await expect(page.locator(`[data-evidence-id="runs.run.${failedRunId}"]`)).toBeVisible();
  await page.locator(`[data-evidence-id="runs.run.${failedRunId}"]`).click();
  await expect(page.getByText("Failure", { exact: true })).toBeVisible();
  await expect(page.getByText(/reason: node_execution_failed/)).toBeVisible();
  const evidenceBefore = await (await request.get(`${apiBase}/v1/runs/${failedRunId}/evidence`, {
    headers: { "X-API-Key": apiKey! },
  })).json() as { summary: { audit_count: number } };
  await testInfo.attach("runs-failed-filter-and-detail", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.locator(`[data-evidence-id="runs.action.${failedRunId}.replay"]`).click();
  await expect(page.getByText(`Requeued ${failedRunId}`)).toBeVisible();
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${failedRunId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    return (await response.json() as { status: string }).status;
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe("failed");
  const evidenceAfter = await (await request.get(`${apiBase}/v1/runs/${failedRunId}/evidence`, {
    headers: { "X-API-Key": apiKey! },
  })).json() as { summary: { audit_count: number } };
  expect(evidenceAfter.summary.audit_count).toBeGreaterThan(evidenceBefore.summary.audit_count);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByText(/reason: node_execution_failed/)).toBeVisible();
  await testInfo.attach("runs-dead-letter-replayed-and-failed-meaningfully", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await filter.selectOption("all");
  await page.locator(`[data-evidence-id="runs.run.${successfulRunId}"]`).click();
  await expect(page.locator(`[data-evidence-id="runs.action.${successfulRunId}.replay"]`)).toHaveCount(0);
  await page.locator(`[data-evidence-id="runs.evidence.${successfulRunId}.verify-chain"]`).click();
  await expect(page.getByText(/chain intact · signatures valid/)).toBeVisible();
  await page.locator(`[data-evidence-id="runs.evidence.${successfulRunId}.toggle-raw"]`).click();
  await expect(page.getByText("Metadata-only evidence JSON", { exact: true })).toBeVisible();

  const pageText = await page.locator("main").innerText();
  expect(pageText).toContain('curl -fsS -X POST');
  expect(pageText).toContain('X-API-Key: $ZEROTH_API_KEY');
  expect(pageText).toContain(`"campaign_id": "${health.campaign_id}"`);
  expect(pageText).not.toContain(apiKey!);
  expect(pageText).not.toContain("<run_id>");
  await testInfo.attach("runs-signed-evidence-and-safe-invoke", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await attachSafeJson(testInfo, "runs-operations-result", {
    successful_run_id: successfulRunId,
    failed_run_id: failedRunId,
    replayed_run_id: failedRunId,
    replay_audit_count_before: evidenceBefore.summary.audit_count,
    replay_audit_count_after: evidenceAfter.summary.audit_count,
    deployment_ref: deploymentRef,
    graph_version_ref: graphVersionRef,
    audit_chain: "signed-and-verified",
    provider_cost_usd: 0,
  });
});
