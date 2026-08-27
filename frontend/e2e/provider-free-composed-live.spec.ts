import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const parentWorkflowId = process.env.ZEROTH_EVALUATION_COMPOSED_PARENT_WORKFLOW_ID;
const parentDeploymentRef = process.env.ZEROTH_EVALUATION_COMPOSED_PARENT_DEPLOYMENT_REF;
const parentGraphVersion = process.env.ZEROTH_EVALUATION_COMPOSED_PARENT_GRAPH_VERSION;
const childDeploymentRef = process.env.ZEROTH_EVALUATION_COMPOSED_CHILD_DEPLOYMENT_REF;
const payload = process.env.ZEROTH_EVALUATION_COMPOSED_PAYLOAD;

type RunStatus = {
  run_id: string;
  thread_id: string;
  parent_run_id: string | null;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output: Record<string, unknown> | null;
};

type ChildRun = {
  run_id: string;
  thread_id: string;
  parent_run_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
};

type RunEconomics = {
  summary: {
    priced_call_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
    reconciliation_state: string;
  };
};

async function waitForTerminal(request: APIRequestContext, runId: string): Promise<RunStatus> {
  const headers = { "X-API-Key": apiKey!, "X-Tenant-ID": tenant };
  let latest: RunStatus | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, { headers });
    expect(response.status()).toBe(200);
    latest = await response.json() as RunStatus;
    return latest.status;
  }, { timeout: 60_000, intervals: [200, 400, 800] }).toBe("succeeded");
  return latest!;
}

async function openRunDialog(page: Page): Promise<Locator> {
  const dock = page.locator(".studio-run-dock");
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Run workflow" });
  await expect(dialog.getByRole("textbox", { name: /Input payload/ })).toBeVisible();
  return dialog;
}

test.describe("provider-free batching and subgraphs", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(
    !parentWorkflowId || !parentDeploymentRef || !parentGraphVersion || !childDeploymentRef || !payload,
    "requires the provider-free composed fixture manifest",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("provider-free composed fixture runs three times with durable lineage", async ({ page, request }, testInfo) => {
    test.setTimeout(150_000);
    coverCriteria(
      testInfo,
      "batching.real-eight-items-concurrency-four",
      "batching.live-studio-three-repetitions",
      "subgraphs.persisted-child-lineage",
      "subgraphs.persisted-cost-rollup",
      "runs.refresh-restoration",
    );
    const headers = { "X-API-Key": apiKey!, "X-Tenant-ID": tenant };
    const healthResponse = await request.get(`${apiBase}/health`);
    expect(healthResponse.status()).toBe(200);
    const health = await healthResponse.json() as {
      deployment_ref: string;
      graph_version_ref: string;
    };
    expect(health.deployment_ref).toBe(parentDeploymentRef);
    expect(health.graph_version_ref).toBe(parentGraphVersion);

    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(parentWorkflowId!)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
    await page.locator('.react-flow__node[data-id="batch-input"]').click();
    const batchDialog = page.getByRole("dialog", { name: "Edit Eight-item batch" });
    await batchDialog.getByRole("button", { name: "Execution" }).click();
    await expect(batchDialog.getByLabel("Maximum concurrency")).toHaveValue("4");
    await expect(batchDialog.getByLabel("Batch size")).toHaveValue("8");
    await page.keyboard.press("Escape");
    await page.locator('.react-flow__node[data-id="deterministic-child"]').click();
    const childDialog = page.getByRole("dialog", { name: "Edit Deterministic child" });
    await expect(childDialog.getByLabel("Graph ref")).toHaveValue(childDeploymentRef!);
    await page.keyboard.press("Escape");
    const runDialog = await openRunDialog(page);

    const repetitions: Array<Record<string, unknown>> = [];
    for (let repetition = 1; repetition <= 3; repetition += 1) {
      await runDialog.getByRole("textbox", { name: /Input payload/ }).fill(payload!);
      const submitted = page.waitForResponse((response) =>
        response.url().endsWith("/v1/runs") && response.request().method() === "POST"
      );
      await runDialog.getByRole("button", { name: "Run", exact: true }).click();
      const submission = await submitted;
      expect(submission.status()).toBe(202);
      const created = await submission.json() as { run_id: string };
      const parent = await waitForTerminal(request, created.run_id);
      expect(parent.parent_run_id).toBeNull();
      expect(parent.deployment_ref).toBe(parentDeploymentRef);
      expect(parent.graph_version_ref).toBe(parentGraphVersion);
      expect(parent.terminal_output).toEqual(JSON.parse(payload!));

      const childResponse = await request.get(
        `${apiBase}/v1/runs/${encodeURIComponent(parent.run_id)}/children`,
        { headers },
      );
      expect(childResponse.status()).toBe(200);
      const children = await childResponse.json() as ChildRun[];
      expect(children).toHaveLength(8);
      expect(new Set(children.map((child) => child.run_id)).size).toBe(8);
      expect(new Set(children.map((child) => child.thread_id)).size).toBe(8);
      expect(children.every((child) => child.parent_run_id === parent.run_id)).toBe(true);
      expect(children.every((child) => child.deployment_ref === childDeploymentRef)).toBe(true);

      const evidenceResponse = await request.get(
        `${apiBase}/v1/runs/${encodeURIComponent(parent.run_id)}/evidence`,
        { headers },
      );
      expect(evidenceResponse.status()).toBe(200);
      const evidence = await evidenceResponse.json() as RunEconomics;
      expect(evidence.summary.priced_call_count).toBe(0);
      expect(evidence.summary.total_cost_usd).toBe(0);
      expect(evidence.summary.cost_identity_state).toBe("not_applicable_no_priced_call");
      repetitions.push({
        repetition,
        parent_run_id: parent.run_id,
        parent_thread_id: parent.thread_id,
        terminal_output: parent.terminal_output,
        children: children.map((child) => ({
          run_id: child.run_id,
          thread_id: child.thread_id,
          parent_run_id: child.parent_run_id,
        })),
        economics: {
          priced_call_count: evidence.summary.priced_call_count,
          total_cost_usd: evidence.summary.total_cost_usd,
        },
      });
      await expect(runDialog.getByText(/^succeeded$/i)).toBeVisible({ timeout: 10_000 });
      if (repetition < 3) await runDialog.getByRole("button", { name: "Clear" }).click();
    }

    const lastRunId = repetitions[2].parent_run_id as string;
    await page.reload({ waitUntil: "networkidle" });
    const restoredDialog = page.getByRole("dialog", { name: "Run workflow" });
    await expect(restoredDialog.getByText(/^succeeded$/i)).toBeVisible({ timeout: 15_000 });
    await expect(restoredDialog.getByText(lastRunId, { exact: true })).toBeVisible();

    await page.goto(`/console/runs/?run=${encodeURIComponent(lastRunId)}`, { waitUntil: "networkidle" });
    const lineage = page.locator('[data-evidence-id="runs.lineage.children"]');
    await expect(lineage).toContainText("Child runs (8)");
    const firstChildId = (repetitions[2].children as Array<{ run_id: string }>)[0].run_id;
    await page.locator(`[data-evidence-id="runs.lineage.child.${firstChildId}"]`).click();
    const parentLineage = page.locator('[data-evidence-id="runs.lineage.parent"]');
    await expect(parentLineage).toContainText(lastRunId);
    await parentLineage.getByRole("button", { name: lastRunId }).click();
    await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText("Child runs (8)");

    await testInfo.attach("provider-free-composed-ui", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "provider-free-composed-summary", {
      schema_version: 1,
      health,
      repetitions,
      restored_run_id: lastRunId,
      provider_economics_status: "blocked",
    });
  });
});
