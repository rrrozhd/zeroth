import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = process.env.ZEROTH_EVALUATION_LOOP_WORKFLOW_ID;
const workflowName = process.env.ZEROTH_EVALUATION_LOOP_WORKFLOW_NAME;
const deploymentRef = process.env.ZEROTH_EVALUATION_LOOP_DEPLOYMENT_REF;
const payload = process.env.ZEROTH_EVALUATION_LOOP_PAYLOAD;
const loopKey = process.env.ZEROTH_EVALUATION_LOOP_KEY;
const limitPayload = process.env.ZEROTH_EVALUATION_LOOP_LIMIT_PAYLOAD;

type RunStatus = {
  run_id: string;
  status: string;
  deployment_ref: string;
  terminal_output: Record<string, unknown> | null;
  audit_refs: string[];
  traversal: {
    node_visit_counts: Record<string, number>;
    edge_visit_counts: Record<string, number>;
    routing_decisions: Array<{ selected_edge_id: string | null }>;
  } | null;
};

type RunReconciliation = {
  chain: {
    verified: boolean;
    signature_verified: boolean;
    record_count: number;
    unsigned_record_count: number;
    signing_key_id: string | null;
  };
  audit_count: number;
  provider_call_count: number;
  total_cost_usd: number;
  measurements: string[];
};

async function waitForTerminal(request: APIRequestContext, runId: string): Promise<RunStatus> {
  const headers = { "X-API-Key": apiKey! };
  let latest: RunStatus | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, { headers });
    expect(response.status()).toBe(200);
    latest = await response.json() as RunStatus;
    return latest.status;
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toMatch(/^(succeeded|failed|cancelled|ambiguous)$/);
  return latest!;
}

async function openRunPanel(page: Page): Promise<void> {
  const dock = page.locator(".studio-run-dock");
  if (await dock.getByRole("textbox", { name: /Input payload/ }).count()) return;
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  await expect(dock.getByRole("textbox", { name: /Input payload/ })).toBeVisible();
}

async function submitFromStudio(
  page: Page,
  request: APIRequestContext,
  input: string,
): Promise<RunStatus> {
  const dock = page.locator(".studio-run-dock");
  const inputBox = dock.getByRole("textbox", { name: /Input payload/ });
  await inputBox.fill(input);
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST",
  );
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  const body = await response.json() as { run_id: string };
  return waitForTerminal(request, body.run_id);
}

async function reconcileRun(request: APIRequestContext, run: RunStatus): Promise<RunReconciliation> {
  const headers = { "X-API-Key": apiKey! };
  const chainResponse = await request.post(`${apiBase}/v1/runs/${run.run_id}/verify-chain`, {
    headers,
    data: {},
  });
  expect(chainResponse.status()).toBe(200);
  const chain = await chainResponse.json() as RunReconciliation["chain"];
  expect(chain).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });

  const evidenceResponse = await request.get(`${apiBase}/v1/runs/${run.run_id}/evidence`, { headers });
  expect(evidenceResponse.status()).toBe(200);
  const evidence = await evidenceResponse.json() as {
    audits: Array<{
      cost_usd: number | null;
      estimated_cost_usd: number | null;
      cost_measurement: string | null;
      token_usage: unknown;
    }>;
    summary: { audit_count: number; tool_call_count: number };
  };
  expect(evidence.summary.audit_count).toBe(run.audit_refs.length);
  expect(evidence.audits.every((audit) => audit.cost_usd === 0)).toBe(true);
  expect(evidence.audits.every((audit) => audit.estimated_cost_usd === 0)).toBe(true);
  expect(evidence.audits.every((audit) => audit.token_usage == null)).toBe(true);
  return {
    chain,
    audit_count: evidence.summary.audit_count,
    provider_call_count: 0,
    total_cost_usd: evidence.audits.reduce((sum, audit) => sum + (audit.cost_usd ?? 0), 0),
    measurements: Array.from(new Set(evidence.audits.map((audit) => audit.cost_measurement ?? "none"))),
  };
}

async function attachRunEvidence(
  testInfo: TestInfo,
  page: Page,
  label: string,
  run: RunStatus,
  reconciliation: RunReconciliation,
) {
  await testInfo.attach(label, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, `${label}-runtime`, {
    run_id: run.run_id,
    status: run.status,
    deployment_ref: run.deployment_ref,
    terminal_output: run.terminal_output,
    audit_refs: run.audit_refs,
    traversal: run.traversal,
    reconciliation,
  });
}

test.describe("manifest-backed loop execution from Studio", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(!workflowId || !workflowName || !deploymentRef || !payload || !loopKey, "requires a selected loop fixture");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("three repetitions expose Repeat and Done with durable run evidence", async ({ page, request }, testInfo) => {
    test.setTimeout(90_000);
    coverCriteria(
      testInfo,
      "loops.three-repetitions",
      "loops.repeat-done",
      "manifests.execution",
      "runs.refresh-restoration",
    );
    const health = await (await request.get(`${apiBase}/health`)).json() as {
      deployment_ref: string;
      graph_version_ref: string;
    };
    expect(health.deployment_ref).toBe(deploymentRef);
    expect(health.graph_version_ref.startsWith(`${workflowId}@`)).toBe(true);

    await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
    await expect(page.getByLabel("Workflow name")).toHaveValue(workflowName!);
    await expect(page.getByText("Repeat", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Done", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Limit", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("1 attempt + 2 retries", { exact: true })).toBeVisible();
    await openRunPanel(page);

    const repetitions: RunStatus[] = [];
    for (let index = 1; index <= 3; index += 1) {
      const run = await submitFromStudio(page, request, payload!);
      repetitions.push(run);
      expect(run.status).toBe("succeeded");
      expect(run.deployment_ref).toBe(deploymentRef);
      expect(run.audit_refs.length).toBeGreaterThan(0);
      const loop = (run.terminal_output?.zeroth_loop as Record<string, Record<string, unknown>> | undefined)?.[loopKey!];
      expect(loop?.route).toBe("done");
      expect(loop?.retries_used).toBeGreaterThanOrEqual(1);
      expect(run.traversal?.routing_decisions.some((decision) =>
        decision.selected_edge_id?.includes("repeat"),
      )).toBe(true);
      expect(run.traversal?.routing_decisions.some((decision) =>
        decision.selected_edge_id?.includes("done"),
      )).toBe(true);
      const reconciliation = await reconcileRun(request, run);
      await expect(page.locator(".studio-run-dock").getByText(/^succeeded$/i)).toBeVisible({ timeout: 10_000 });
      await attachRunEvidence(testInfo, page, `loop-happy-repetition-${index}`, run, reconciliation);
      if (index < 3) await page.locator(".studio-run-dock").getByRole("button", { name: "Clear" }).click();
    }

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator(".studio-run-dock").getByText(/^succeeded$/i)).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(repetitions.at(-1)!.run_id, { exact: true })).toBeVisible();
    await testInfo.attach("loop-run-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });

  test("malformed JSON is rejected locally without creating a run", async ({ page, request }, testInfo) => {
    coverCriteria(testInfo, "fields.run-payload-json", "loops.malformed-input");
    const headers = { "X-API-Key": apiKey! };
    const before = await (await request.get(`${apiBase}/v1/admin/runs`, { headers })).json() as { total: number };
    await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
    await openRunPanel(page);
    const dock = page.locator(".studio-run-dock");
    await dock.getByRole("textbox", { name: /Input payload/ }).fill("{");
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    await expect(dock.getByText("Input payload is not valid JSON.", { exact: true })).toBeVisible();
    const after = await (await request.get(`${apiBase}/v1/admin/runs`, { headers })).json() as { total: number };
    expect(after.total).toBe(before.total);
    await testInfo.attach("loop-malformed-json-rejected", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });

  test("max retries exits through Limit without exceeding the bound", async ({ page, request }, testInfo) => {
    test.skip(!limitPayload, "this loop fixture has no deterministic Limit payload");
    test.setTimeout(45_000);
    coverCriteria(testInfo, "loops.limit-route", "loops.max-retries");
    await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
    await openRunPanel(page);
    const run = await submitFromStudio(page, request, limitPayload!);
    expect(run.status).toBe("succeeded");
    const loop = (run.terminal_output?.zeroth_loop as Record<string, Record<string, unknown>> | undefined)?.[loopKey!];
    expect(loop).toMatchObject({ route: "limit", max_retries: 2, retries_used: 2 });
    expect((loop?.attempt as number)).toBe(3);
    expect(run.traversal?.routing_decisions.some((decision) =>
      decision.selected_edge_id?.includes("limit"),
    )).toBe(true);
    const reconciliation = await reconcileRun(request, run);
    await attachRunEvidence(testInfo, page, "loop-limit-after-max-retries", run, reconciliation);
  });
});
