import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = "evaluation-studio-v1-governed-remediation";
const deploymentRef = "evaluation-studio-v1-governed-remediation-v2";

test("a pending governed run cancels through Runs without executing the action", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(testInfo, "runs.cancel", "runs.failure-display", "approvals.cancel-before-resolution");
  await configurePage(page, apiBase, tenant, apiKey!);

  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string;
    graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    graph_version_ref: `${workflowId}@2`,
  });

  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
  const dock = page.locator(".studio-run-dock");
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  await dock.getByRole("textbox", { name: /Input payload/ }).fill(JSON.stringify({
    ticket: `synthetic-runs-cancel-${Date.now()}`,
    status: "remediated",
  }));
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST",
  );
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  const createResponse = await submitted;
  expect(createResponse.status()).toBe(202);
  const { run_id: runId } = await createResponse.json() as { run_id: string };

  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    return (await response.json() as { status: string }).status;
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe("paused_for_approval");

  await page.goto(`/console/runs/?run=${runId}`, { waitUntil: "networkidle" });
  const cancel = page.locator(`[data-evidence-id="runs.action.${runId}.cancel"]`);
  await expect(cancel).toBeVisible();
  await expect(page.locator(`[data-evidence-id="runs.action.${runId}.interrupt"]`)).toHaveCount(0);
  await expect(page.getByText(/Held for approval at node approval/)).toBeVisible();
  await testInfo.attach("run-cancel-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  page.once("dialog", (dialog) => dialog.accept());
  await cancel.click();
  await expect(page.getByText(`Cancelled ${runId}`)).toBeVisible();
  let terminal: { status: string; failure_state: { reason: string } | null } | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    terminal = await response.json() as typeof terminal;
    return terminal!.status;
  }).toBe("failed");
  expect(terminal!.failure_state?.reason).toBe("operator_cancelled");

  await page.reload({ waitUntil: "networkidle" });
  await page.locator('[data-evidence-id="runs.filter.status"]').selectOption("failed");
  await expect(page.locator(`[data-evidence-id="runs.run.${runId}"]`)).toBeVisible();
  await expect(page.getByText(/reason: operator_cancelled/)).toBeVisible();
  const evidence = await (await request.get(`${apiBase}/v1/runs/${runId}/evidence`, {
    headers: { "X-API-Key": apiKey! },
  })).json() as {
    summary: { audit_count: number; approval_count: number };
    approvals: Array<{ status: string; resolution: unknown }>;
  };
  expect(evidence.summary.approval_count).toBe(1);
  expect(evidence.approvals).toHaveLength(1);
  expect(evidence.approvals[0].resolution).toBeNull();
  await testInfo.attach("run-cancelled-and-restored", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "run-cancel-result", {
    run_id: runId,
    status: terminal!.status,
    failure_reason: terminal!.failure_state?.reason,
    approval_count: evidence.summary.approval_count,
    approval_resolution: null,
    action_executed: false,
  });
});
