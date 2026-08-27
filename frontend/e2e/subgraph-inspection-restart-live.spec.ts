import { expect, test, type APIRequestContext } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const parentRunId = process.env.ZEROTH_EVALUATION_SUBGRAPH_PARENT_RUN_ID;
const deploymentRef = process.env.ZEROTH_EVALUATION_SUBGRAPH_PARENT_DEPLOYMENT_REF;
const graphVersionRef = process.env.ZEROTH_EVALUATION_SUBGRAPH_PARENT_GRAPH_VERSION;

type RunStatus = {
  run_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  thread_id: string;
  parent_run_id: string | null;
};

type ChildRun = RunStatus & { parent_run_id: string };

async function apiProjection(request: APIRequestContext): Promise<{
  health: Record<string, unknown>;
  parent: RunStatus;
  children: ChildRun[];
}> {
  const headers = { "X-API-Key": apiKey!, "X-Tenant-ID": tenant };
  const [healthResponse, parentResponse, childrenResponse] = await Promise.all([
    request.get(`${apiBase}/health`),
    request.get(`${apiBase}/v1/runs/${encodeURIComponent(parentRunId!)}`, { headers }),
    request.get(`${apiBase}/v1/runs/${encodeURIComponent(parentRunId!)}/children`, { headers }),
  ]);
  expect(healthResponse.status()).toBe(200);
  expect(parentResponse.status()).toBe(200);
  expect(childrenResponse.status()).toBe(200);
  return {
    health: await healthResponse.json() as Record<string, unknown>,
    parent: await parentResponse.json() as RunStatus,
    children: await childrenResponse.json() as ChildRun[],
  };
}

test.describe("provider-free subgraph restart inspection", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(
    !apiKey || !parentRunId || !deploymentRef || !graphVersionRef,
    "requires the post-restart parent identity",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("inspects restored parent and child lineage after backend restart", async ({ page, request }, testInfo) => {
    coverCriteria(
      testInfo,
      "subgraphs.live-studio-child-run-inspection",
      "subgraphs.restart-restoration-in-served-parent",
    );
    const projection = await apiProjection(request);
    expect(projection.health.deployment_ref).toBe(deploymentRef);
    expect(projection.health.graph_version_ref).toBe(graphVersionRef);
    expect(projection.parent).toMatchObject({
      run_id: parentRunId,
      status: "succeeded",
      deployment_ref: deploymentRef,
      graph_version_ref: graphVersionRef,
      parent_run_id: null,
    });
    expect(projection.children).toHaveLength(8);
    expect(new Set(projection.children.map((child) => child.run_id)).size).toBe(8);
    expect(new Set(projection.children.map((child) => child.thread_id)).size).toBe(8);
    expect(projection.children.every((child) => child.parent_run_id === parentRunId)).toBe(true);
    expect(projection.children.every((child) => child.status === "succeeded")).toBe(true);

    await page.goto(`/console/runs/?run=${encodeURIComponent(parentRunId!)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
    const lineage = page.locator('[data-evidence-id="runs.lineage.children"]');
    await expect(lineage).toContainText("Child runs (8)");
    await expect(
      page.getByText("Node timeline", { exact: true }).locator("..").locator(".z-pulse"),
    ).toHaveCount(0);
    await expect(
      page.getByLabel("Run details").getByText(parentRunId!, { exact: true }),
    ).toBeVisible();
    await testInfo.attach("restart-restored-parent-lineage", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const child = projection.children[3];
    await page.locator(`[data-evidence-id="runs.lineage.child.${child.run_id}"]`).click();
    const parentLineage = page.locator('[data-evidence-id="runs.lineage.parent"]');
    await expect(parentLineage).toContainText(parentRunId!);
    await expect(
      page.getByText("Node timeline", { exact: true }).locator("..").locator(".z-pulse"),
    ).toHaveCount(0);
    await testInfo.attach("restored-child-parent-inspection", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await parentLineage.getByRole("button", { name: parentRunId! }).click();
    await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText("Child runs (8)");
    await attachSafeJson(testInfo, "subgraph-restart-inspection-summary", {
      schema_version: 1,
      health: {
        status: projection.health.status,
        campaign_id: projection.health.campaign_id,
        deployment_ref: projection.health.deployment_ref,
        deployment_version: projection.health.deployment_version,
        graph_version_ref: projection.health.graph_version_ref,
      },
      parent: projection.parent,
      children: projection.children,
      inspected_child_run_id: child.run_id,
      provider_calls_performed: 0,
    });
  });
});
