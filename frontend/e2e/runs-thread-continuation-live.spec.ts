import { expect, test, type APIRequestContext } from "@playwright/test";

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

async function waitForSuccess(request: APIRequestContext, runId: string) {
  let run: { run_id: string; thread_id: string; status: string } | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    run = await response.json() as typeof run;
    return run!.status;
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe("succeeded");
  return run!;
}

test("Studio retains a minted thread and continues it in a distinct second run", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(
    testInfo,
    "runs.thread-continuation",
    "runs.refresh-restoration",
    "runs.thread-continuation-refresh",
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
  await expect(dock).toBeVisible({ timeout: 15_000 });
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  const runDialog = page.getByRole("dialog", { name: "Run" });
  await expect(runDialog).toBeVisible();
  const payload = runDialog.getByRole("textbox", { name: /Input payload/ });
  const thread = runDialog.getByRole("textbox", { name: "Thread", exact: true });
  await expect(thread).toHaveValue("");

  await payload.fill(JSON.stringify({
    records: [{ name: "Grace", email: "grace@example.test", status: "active" }],
  }));
  const firstSubmitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST",
  );
  await runDialog.getByRole("button", { name: "Run", exact: true }).click();
  const firstCreate = await firstSubmitted;
  expect(firstCreate.status()).toBe(202);
  const firstResponse = await firstCreate.json() as { run_id: string; thread_id: string };
  const first = await waitForSuccess(request, firstResponse.run_id);
  expect(first.thread_id).toBeTruthy();
  await expect(thread).toHaveValue(first.thread_id);

  await runDialog.getByRole("button", { name: "Clear", exact: true }).click();
  await expect(thread).toHaveValue(first.thread_id);
  await payload.fill(JSON.stringify({
    records: [{ name: "Katherine", email: "katherine@example.test", status: "pending" }],
  }));
  await testInfo.attach("thread-retained-before-continuation", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const secondSubmitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST",
  );
  await runDialog.getByRole("button", { name: "Run", exact: true }).click();
  const secondCreate = await secondSubmitted;
  expect(secondCreate.status()).toBe(202);
  const secondResponse = await secondCreate.json() as { run_id: string; thread_id: string };
  const second = await waitForSuccess(request, secondResponse.run_id);
  expect(second.run_id).not.toBe(first.run_id);
  expect(second.thread_id).toBe(first.thread_id);
  expect(secondResponse.thread_id).toBe(first.thread_id);

  await page.goto(`/console/runs/?run=${second.run_id}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByText(second.run_id, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(first.thread_id, { exact: true }).first()).toBeVisible();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByText(second.run_id, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(first.thread_id, { exact: true }).first()).toBeVisible();
  await testInfo.attach("thread-continuation-restored-in-runs", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "thread-continuation-result", {
    thread_id: first.thread_id,
    first_run_id: first.run_id,
    second_run_id: second.run_id,
    deployment_ref: deploymentRef,
    graph_version_ref: `${workflowId}@${workflowVersion}`,
    distinct_runs_same_thread: true,
    provider_cost_usd: 0,
  });
});
