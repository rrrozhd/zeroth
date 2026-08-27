import { expect, test, type APIRequestContext } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const stage = process.env.ZEROTH_EVALUATION_DEPLOYMENT_STAGE ?? "none";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8125";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const deploymentRef = process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF
  ?? "validation-runs-deployments-20260825-v1";
const graphId = process.env.ZEROTH_EVALUATION_GRAPH_ID
  ?? "43dc0a14-e924-4d3e-8763-740408ebee3a";

type Deployment = {
  deployment_ref: string;
  version: number;
  graph_version_ref: string;
  status: string;
  serving: boolean;
};

async function history(request: APIRequestContext) {
  const response = await request.get(`${apiBase}/v1/deployments`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  return (await response.json() as Deployment[])
    .filter((deployment) => deployment.deployment_ref === deploymentRef)
    .sort((left, right) => right.version - left.version);
}

test.beforeEach(async ({ page }, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  await configurePage(page, apiBase, tenant, apiKey!);
});

test("register the disposable deployment through the UI", async ({ page, request }, testInfo) => {
  test.skip(stage !== "create", "not the requested deployment stage");
  coverCriteria(testInfo, "deployments.create", "deployments.persistence");
  expect(await history(request)).toHaveLength(0);

  await page.goto("/console/deployments/", { waitUntil: "domcontentloaded" });
  await page.locator('[data-evidence-id="deployments.new.open"]').click();
  await page.locator('[data-evidence-id="deployments.new.deployment-ref"]').fill(deploymentRef);
  await page.locator('[data-evidence-id="deployments.new.graph-id"]').fill(graphId);
  await page.locator('[data-evidence-id="deployments.new.graph-version"]').fill("2");
  await testInfo.attach("deployment-v1-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.locator('[data-evidence-id="deployments.new.submit"]').click();
  await expect(page.getByText(`Registered ${deploymentRef} · ${graphId}@2`)).toBeVisible();
  await expect.poll(async () => (await history(request))[0]?.version).toBe(1);
  const created = (await history(request))[0];
  expect(created).toMatchObject({ version: 1, graph_version_ref: `${graphId}@2`, serving: false });
  await testInfo.attach("deployment-v1-registered", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "deployment-v1-result", created);
});

test("inspect the exact deployment version after restart", async ({ page, request }, testInfo) => {
  test.skip(stage !== "verify-initial", "not the requested deployment stage");
  coverCriteria(testInfo, "deployments.inspect", "deployments.restart-serving", "deployments.persistence");
  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string; deployment_version: number; graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    deployment_version: 1,
    graph_version_ref: `${graphId}@2`,
  });
  await page.goto("/console/deployments/", { waitUntil: "domcontentloaded" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-1"]`).click();
  await expect(page.getByText("serving", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(`${graphId}@2`, { exact: true }).first()).toBeVisible();
  await testInfo.attach("deployment-v1-serving-inspected", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "deployment-v1-serving-health", health);
});

test("register rollback to graph v1 through the UI", async ({ page, request }, testInfo) => {
  test.skip(stage !== "rollback", "not the requested deployment stage");
  coverCriteria(testInfo, "deployments.rollback", "deployments.persistence");
  await page.goto("/console/deployments/", { waitUntil: "domcontentloaded" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-1"]`).click();
  await page.locator(`[data-evidence-id="deployments.rollback.${deploymentRef}.open"]`).click();
  await page.locator(`[data-evidence-id="deployments.rollback.${deploymentRef}.target-version"]`).fill("1");
  await testInfo.attach("deployment-rollback-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.locator(`[data-evidence-id="deployments.rollback.${deploymentRef}.confirm"]`).click();
  await expect(page.getByText(`Registered rollback of ${deploymentRef} to graph v1`)).toBeVisible();
  await expect.poll(async () => (await history(request))[0]?.version).toBe(2);
  const rolledBack = (await history(request))[0];
  expect(rolledBack).toMatchObject({ version: 2, graph_version_ref: `${graphId}@1`, serving: false });
  await testInfo.attach("deployment-rollback-registered", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "deployment-rollback-result", rolledBack);
});

test("verify rollback is serving and historical versions remain", async ({ page, request }, testInfo) => {
  test.skip(stage !== "verify-rollback", "not the requested deployment stage");
  coverCriteria(testInfo, "deployments.rollback", "deployments.restart-serving", "deployments.persistence");
  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string; deployment_version: number; graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    deployment_version: 2,
    graph_version_ref: `${graphId}@1`,
  });
  const deployments = await history(request);
  expect(deployments.map((item) => [item.version, item.graph_version_ref])).toEqual([
    [2, `${graphId}@1`],
    [1, `${graphId}@2`],
  ]);
  await page.goto("/console/deployments/", { waitUntil: "domcontentloaded" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-2"]`).click();
  await expect(page.getByText("serving", { exact: true }).first()).toBeVisible();
  await testInfo.attach("deployment-rollback-serving", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "deployment-rollback-serving-health", { health, deployments });
});

test("register roll-forward to graph v2 through the UI", async ({ page, request }, testInfo) => {
  test.skip(stage !== "rollforward", "not the requested deployment stage");
  coverCriteria(testInfo, "deployments.rollforward", "deployments.persistence");
  await page.goto("/console/deployments/", { waitUntil: "domcontentloaded" });
  await page.locator('[data-evidence-id="deployments.new.open"]').click();
  await page.locator('[data-evidence-id="deployments.new.deployment-ref"]').fill(deploymentRef);
  await page.locator('[data-evidence-id="deployments.new.graph-id"]').fill(graphId);
  await page.locator('[data-evidence-id="deployments.new.graph-version"]').fill("2");
  await testInfo.attach("deployment-rollforward-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.locator('[data-evidence-id="deployments.new.submit"]').click();
  await expect(page.getByText(`Registered ${deploymentRef} · ${graphId}@2`)).toBeVisible();
  await expect.poll(async () => (await history(request))[0]?.version).toBe(3);
  const forward = (await history(request))[0];
  expect(forward).toMatchObject({ version: 3, graph_version_ref: `${graphId}@2`, serving: false });
  await testInfo.attach("deployment-rollforward-registered", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "deployment-rollforward-result", forward);
});

test("verify roll-forward is serving with complete history", async ({ page, request }, testInfo) => {
  test.skip(stage !== "verify-rollforward", "not the requested deployment stage");
  coverCriteria(testInfo, "deployments.rollforward", "deployments.restart-serving", "deployments.persistence");
  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string; deployment_version: number; graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    deployment_version: 3,
    graph_version_ref: `${graphId}@2`,
  });
  const deployments = await history(request);
  expect(deployments.map((item) => [item.version, item.graph_version_ref])).toEqual([
    [3, `${graphId}@2`],
    [2, `${graphId}@1`],
    [1, `${graphId}@2`],
  ]);
  await page.goto("/console/deployments/", { waitUntil: "domcontentloaded" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-3"]`).click();
  await expect(page.getByText("serving", { exact: true }).first()).toBeVisible();
  await testInfo.attach("deployment-rollforward-serving-history", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  const runsResponse = await request.get(`${apiBase}/v1/admin/runs`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(runsResponse.status()).toBe(200);
  const runs = await runsResponse.json() as {
    runs: Array<{ run_id: string; status: string; deployment_ref: string; graph_version_ref: string }>;
  };
  const historical = runs.runs.find((run) =>
    run.deployment_ref === deploymentRef
      && run.graph_version_ref === `${graphId}@1`
      && run.status === "succeeded",
  );
  expect(historical, "the run created while rollback v1 was serving must persist").toBeTruthy();
  await page.goto(`/console/runs/?run=${historical!.run_id}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByText(historical!.run_id, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(`${graphId}@1`, { exact: true }).first()).toBeVisible();
  await testInfo.attach("deployment-rollforward-historical-run-retained", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "deployment-rollforward-serving-health", {
    health,
    deployments,
    historical_run_id: historical!.run_id,
    historical_graph_version_ref: historical!.graph_version_ref,
  });
});
