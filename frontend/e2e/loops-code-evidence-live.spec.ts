import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;

const qualityLoop = "43dc0a14-e924-4d3e-8763-740408ebee3a";
const incidentLoop = "da5da69b-1086-4cfe-8090-424a0118b88c";
const successWorkflow = "2f2b20b2-8acc-4488-9a01-71b1b4f088f6";
const missingManifestWorkflow = "d9870a11-c8bb-4705-9583-0f638fb97e9b";

const successRun = "821d49cc49094243866129e739d5b5ff";
const malformedRun = "0aea4396a122496b9aeee1e3482359c0";
const timeoutRun = "8e2c7725c0b145aa97e1fd0702fce9fb";

type RunStatus = {
  run_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output: Record<string, unknown> | null;
  failure_state: { reason: string; message: string } | null;
};

async function attachRuntime(
  request: APIRequestContext,
  testInfo: TestInfo,
  runId: string,
): Promise<RunStatus> {
  const headers = { "X-API-Key": apiKey! };
  const runResponse = await request.get(`${apiBase}/v1/runs/${runId}`, { headers });
  expect(runResponse.status()).toBe(200);
  const run = await runResponse.json() as RunStatus;
  const chainResponse = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
    headers,
    data: {},
  });
  expect(chainResponse.status()).toBe(200);
  const chain = await chainResponse.json() as {
    verified: boolean;
    signature_verified: boolean;
    record_count: number;
    unsigned_record_count: number;
  };
  expect(chain).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });
  const evidenceResponse = await request.get(`${apiBase}/v1/runs/${runId}/evidence`, { headers });
  expect(evidenceResponse.status()).toBe(200);
  const evidence = await evidenceResponse.json() as {
    audits: Array<{
      node_id: string;
      execution_metadata: { manifest_ref_sha256?: string };
      cost_usd: number | null;
      cost_measurement: string | null;
    }>;
    summary: {
      priced_call_count: number;
      cost_event_count: number;
      total_cost_usd: number;
      reconciliation_state: string;
    };
  };
  expect(evidence.summary).toMatchObject({
    priced_call_count: 0,
    cost_event_count: 0,
    total_cost_usd: 0,
    reconciliation_state: "reconciled_zero_activity",
  });
  await attachSafeJson(testInfo, `runtime-${runId}`, {
    run,
    chain,
    economics: evidence.summary,
    node_identities: evidence.audits.map((audit) => ({
      node_id: audit.node_id,
      manifest_ref_sha256: audit.execution_metadata.manifest_ref_sha256 ?? null,
      cost_usd: audit.cost_usd,
      cost_measurement: audit.cost_measurement,
    })),
  });
  return run;
}

async function selectRun(page: Page, runId: string): Promise<void> {
  const row = page.locator(`[data-evidence-id="runs.run.${runId}"]`);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();
  await expect(page.getByText(runId, { exact: true }).last()).toBeVisible();
}

test.describe("provider-independent loop and executable-code evidence", () => {
  test.skip(!liveEnabled, "requires the persistent local evaluation service");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("both demos show a dedicated Loop with Repeat, Done, Limit, and retry count", async ({ page }, testInfo) => {
    coverCriteria(
      testInfo,
      "loops.dedicated-node",
      "loops.repeat-done-limit",
      "loops.max-retries-visible",
      "loops.both-demos",
    );
    for (const [name, workflowId] of [
      ["quality", qualityLoop],
      ["incident", incidentLoop],
    ] as const) {
      await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "domcontentloaded" });
      await expect(page.getByText("Repeat", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
      await expect(page.getByText("Done", { exact: true }).first()).toBeVisible();
      await expect(page.getByText("Limit", { exact: true }).first()).toBeVisible();
      await expect(page.getByText("1 attempt + 2 retries", { exact: true })).toBeVisible();
      await testInfo.attach(`${name}-loop-architecture`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
    }
  });

  test("published inline code is read-only and its successful run is inspectable", async ({ page, request }, testInfo) => {
    coverCriteria(
      testInfo,
      "code.inline-published",
      "code.content-identity",
      "code.read-only",
      "runs.inline-success",
    );
    const run = await attachRuntime(request, testInfo, successRun);
    expect(run).toMatchObject({ status: "succeeded", terminal_output: { validated: true } });

    await page.goto(`/console/studio/edit/?id=${successWorkflow}`, { waitUntil: "domcontentloaded" });
    await expect(page.getByLabel("Workflow name")).toHaveValue("Acceptance inline code success 20260825");
    await expect(page.getByText("published", { exact: true })).toBeVisible();
    await page.getByText("Validate inline payload", { exact: true }).click();
    await expect(page.getByText(/published graphs are immutable/i)).toBeVisible();
    await expect(page.getByText(/frozen and content-hashed at publish/i)).toBeVisible();
    await expect(page.getByRole("spinbutton", { name: /Timeout \(seconds\)/i })).toHaveValue("5");
    await testInfo.attach("published-inline-code-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.goto("/console/runs/", { waitUntil: "domcontentloaded" });
    await selectRun(page, successRun);
    await expect(page.getByText(/"validated": true/)).toBeVisible();
    await testInfo.attach("published-inline-code-run", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });

  test("malformed output and execution timeout remain meaningful in Runs", async ({ page, request }, testInfo) => {
    coverCriteria(testInfo, "code.malformed-output", "code.execution-timeout", "runs.failure-display");
    const malformed = await attachRuntime(request, testInfo, malformedRun);
    const timeout = await attachRuntime(request, testInfo, timeoutRun);
    expect(malformed.failure_state?.message).toContain("stdout is not valid JSON");
    expect(timeout.failure_state?.message).toContain("timed out after 1s");

    await page.goto("/console/runs/", { waitUntil: "domcontentloaded" });
    await page.getByRole("combobox", { name: "Filter runs by status" }).selectOption("failed");
    await selectRun(page, malformedRun);
    await expect(page.getByText(/stdout is not valid JSON/)).toBeVisible();
    await testInfo.attach("inline-malformed-output-failure", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await selectRun(page, timeoutRun);
    await expect(page.getByText(/timed out after 1s/)).toBeVisible();
    await testInfo.attach("inline-timeout-failure", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });

  test("missing registered manifest fails closed in Studio preflight", async ({ page }, testInfo) => {
    coverCriteria(testInfo, "code.manifest-missing", "studio.preflight-error-focus");
    await page.goto(`/console/studio/edit/?id=${missingManifestWorkflow}`, { waitUntil: "domcontentloaded" });
    const preflight = page.getByRole("button", { name: /Preflight/i });
    await expect(preflight).toBeEnabled({ timeout: 20_000 });
    await preflight.click();
    await expect(page.getByText("unresolved_manifest_ref", { exact: true })).toBeVisible();
    await expect(page.getByText(/is not registered/)).toBeVisible();
    await testInfo.attach("missing-manifest-preflight-rejected", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });
});
