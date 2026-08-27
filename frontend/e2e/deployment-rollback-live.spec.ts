import { expect, test, type APIRequestContext } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const stage = process.env.ZEROTH_EVALUATION_DEPLOYMENT_STAGE ?? "none";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const deploymentRef = "evaluation-studio-v1-grounded-researcher-v1";
const graphId = "evaluation-studio-v1-grounded-researcher";
const historicalRunId = process.env.ZEROTH_EVALUATION_HISTORICAL_RUN_ID
  ?? "b542c2e061ba44ee858034c03874797b";

type Deployment = {
  deployment_ref: string;
  version: number;
  graph_version_ref: string;
  serving: boolean;
  status: string;
};

async function deployments(request: APIRequestContext) {
  const response = await request.get(`${apiBase}/v1/deployments`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  return (await response.json() as Deployment[])
    .filter((item) => item.deployment_ref === deploymentRef)
    .sort((a, b) => b.version - a.version);
}

test.beforeEach(async ({ page }, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  await configurePage(page, apiBase, tenant, apiKey!);
});

test("register rollback to graph v1 through Deployments UI", async ({ page, request }, testInfo) => {
  test.skip(stage !== "register-rollback", "not the requested deployment campaign stage");
  coverCriteria(testInfo, "deployments.rollback", "deployments.persistence");

  const before = await deployments(request);
  const current = before[0];
  expect(current).toMatchObject({ version: 2, graph_version_ref: `${graphId}@2`, serving: true });

  await page.goto("/console/deployments/", { waitUntil: "networkidle" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-2"]`).click();
  await page.locator(`[data-evidence-id="deployments.rollback.${deploymentRef}.open"]`).click();
  const target = page.locator(
    `[data-evidence-id="deployments.rollback.${deploymentRef}.target-version"]`,
  );
  await target.fill("1");
  await testInfo.attach("rollback-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.locator(`[data-evidence-id="deployments.rollback.${deploymentRef}.confirm"]`).click();
  await expect(page.getByText(`Registered rollback of ${deploymentRef} to graph v1`)).toBeVisible();

  await expect.poll(async () => (await deployments(request))[0]?.version).toBe(3);
  const after = await deployments(request);
  expect(after[0]).toMatchObject({ version: 3, graph_version_ref: `${graphId}@1`, serving: false });
  await expect(
    page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-3"]`),
  ).toBeVisible();
  await testInfo.attach("rollback-registered", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "rollback-registration", { before: current, after: after[0] });
});

test("serve rolled-back graph v1 and retain historical run", async ({ page, request }, testInfo) => {
  test.skip(stage !== "verify-rollback", "not the requested deployment campaign stage");
  coverCriteria(testInfo, "deployments.rollback", "deployments.persistence", "runs.detail");

  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string;
    deployment_version: number;
    graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    deployment_version: 3,
    graph_version_ref: `${graphId}@1`,
  });

  await page.goto("/console/deployments/", { waitUntil: "networkidle" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-3"]`).click();
  await expect(page.getByText("serving", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(`${graphId}@1`, { exact: true }).first()).toBeVisible();

  await page.goto(`/console/runs/?run=${historicalRunId}`, { waitUntil: "networkidle" });
  await expect(page.getByText(historicalRunId, { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(`^${historicalRunId} succeeded`) })).toBeVisible();
  await testInfo.attach("rollback-serving-history-retained", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "rollback-serving-health", health);
});

test("register roll-forward to graph v2 through Deployments UI", async ({ page, request }, testInfo) => {
  test.skip(stage !== "register-rollforward", "not the requested deployment campaign stage");
  coverCriteria(testInfo, "deployments.create", "deployments.rollforward");

  await page.goto("/console/deployments/", { waitUntil: "networkidle" });
  await page.locator('[data-evidence-id="deployments.new.open"]').click();
  await page.locator('[data-evidence-id="deployments.new.deployment-ref"]').fill(deploymentRef);
  await page.locator('[data-evidence-id="deployments.new.graph-id"]').fill(graphId);
  await page.locator('[data-evidence-id="deployments.new.graph-version"]').fill("2");
  await testInfo.attach("rollforward-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.locator('[data-evidence-id="deployments.new.submit"]').click();
  await expect(page.getByText(`Registered ${deploymentRef} · ${graphId}@2`)).toBeVisible();

  await expect.poll(async () => (await deployments(request))[0]?.version).toBe(4);
  const after = await deployments(request);
  expect(after[0]).toMatchObject({ version: 4, graph_version_ref: `${graphId}@2`, serving: false });
  await testInfo.attach("rollforward-registered", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "rollforward-registration", after[0]);
});

test("serve rolled-forward graph v2 and retain rollback history", async ({ page, request }, testInfo) => {
  test.skip(stage !== "verify-rollforward", "not the requested deployment campaign stage");
  coverCriteria(testInfo, "deployments.rollforward", "deployments.persistence", "runs.detail");

  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string;
    deployment_version: number;
    graph_version_ref: string;
  };
  expect(health).toMatchObject({
    deployment_ref: deploymentRef,
    deployment_version: 4,
    graph_version_ref: `${graphId}@2`,
  });
  const history = await deployments(request);
  expect(history.slice(0, 4).map((item) => [item.version, item.graph_version_ref])).toEqual([
    [4, `${graphId}@2`],
    [3, `${graphId}@1`],
    [2, `${graphId}@2`],
    [1, `${graphId}@1`],
  ]);

  await page.goto("/console/deployments/", { waitUntil: "networkidle" });
  await page.locator(`[data-evidence-id="deployments.deployment.${deploymentRef}.version-4"]`).click();
  await expect(page.getByText("serving", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(`${graphId}@2`, { exact: true }).first()).toBeVisible();
  await testInfo.attach("rollforward-serving-history", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "rollforward-serving-health", { health, history: history.slice(0, 4) });
});
