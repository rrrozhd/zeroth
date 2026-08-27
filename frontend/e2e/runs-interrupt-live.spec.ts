import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = process.env.ZEROTH_EVALUATION_WORKFLOW_ID
  ?? "43dc0a14-e924-4d3e-8763-740408ebee3a";
const workflowVersion = Number(process.env.ZEROTH_EVALUATION_WORKFLOW_VERSION ?? "2");
const deploymentRef = process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF
  ?? "demo-data-quality-repair-loop-manifest-v1";

test("a genuinely running local code unit interrupts and then cancels from Runs", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(
    testInfo,
    "runs.interrupt",
    "runs.cancel-after-interrupt",
    "runs.persistence",
    "runs.interrupt-cancel-late-result-fence",
  );
  await configurePage(page, apiBase, tenant, apiKey!);

  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string;
    graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    graph_version_ref: `${workflowId}@${workflowVersion}`,
  });

  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "domcontentloaded" });
  const dock = page.locator(".studio-run-dock");
  await expect(dock).toBeVisible();
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  const runDialog = page.getByRole("dialog", { name: "Run" });
  await expect(runDialog).toBeVisible();
  await runDialog.getByRole("textbox", { name: /Input payload/ }).fill(JSON.stringify({
    records: [{ name: "Grace", email: "grace@example.test", status: "active" }],
    evaluation_delay_ms: 8_000,
  }));
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST",
  );
  await runDialog.getByRole("button", { name: "Run", exact: true }).click();
  const createResponse = await submitted;
  expect(createResponse.status()).toBe(202);
  const { run_id: runId } = await createResponse.json() as { run_id: string };

  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    return (await response.json() as { status: string }).status;
  }, { timeout: 5_000, intervals: [100, 200, 300] }).toBe("running");

  await page.goto(`/console/runs/?run=${runId}`, { waitUntil: "domcontentloaded" });
  const interrupt = page.locator(`[data-evidence-id="runs.action.${runId}.interrupt"]`);
  await expect(interrupt).toBeVisible({ timeout: 3_000 });
  await expect(page.locator(`[data-evidence-id="runs.action.${runId}.cancel"]`)).toBeVisible();
  await testInfo.attach("run-genuinely-running-before-interrupt", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await interrupt.click();
  await expect(page.getByText(`Interrupted ${runId}`)).toBeVisible();
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    return (await response.json() as { status: string }).status;
  }).toBe("waiting_interrupt");

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("main").getByText("waiting interrupt", { exact: true }).last()).toBeVisible();
  const cancel = page.locator(`[data-evidence-id="runs.action.${runId}.cancel"]`);
  await expect(cancel).toBeVisible();
  await expect(page.locator(`[data-evidence-id="runs.action.${runId}.interrupt"]`)).toHaveCount(0);
  await testInfo.attach("run-paused-at-interrupt", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  page.once("dialog", (dialog) => dialog.accept());
  await cancel.click();
  await expect(page.getByText(`Cancelled ${runId}`)).toBeVisible();
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    const body = await response.json() as { status: string; failure_state: { reason: string } | null };
    return `${body.status}:${body.failure_state?.reason ?? ""}`;
  }).toBe("failed:operator_cancelled");

  // Let the originally-running bounded subprocess finish. The lifecycle fence
  // must prevent its late result from reviving the cancelled run.
  await page.waitForTimeout(8_500);
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    return (await response.json() as { status: string }).status;
  }, { timeout: 3_000, intervals: [250, 500] }).toBe("failed");
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByText(/reason: operator_cancelled/)).toBeVisible();
  await testInfo.attach("run-interrupt-cancel-fence-persisted", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "run-interrupt-result", {
    run_id: runId,
    running_fixture: "bounded-local-code",
    evaluation_delay_ms: 8_000,
    interrupted_status: "waiting_interrupt",
    terminal_status: "failed",
    terminal_reason: "operator_cancelled",
    late_completion_fenced: true,
    provider_cost_usd: 0,
  });
});
