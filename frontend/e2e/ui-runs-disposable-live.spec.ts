import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

import {
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8130";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const authoringWorkflowId = process.env.ZEROTH_EVALUATION_AUTHORING_WORKFLOW_ID;
const configurationWorkflowId = process.env.ZEROTH_EVALUATION_CONFIGURATION_WORKFLOW_ID;

function headers() {
  return { "X-API-Key": apiKey!, "X-Tenant-ID": tenant };
}

async function workflow(request: APIRequestContext, workflowId: string) {
  const response = await request.get(
    `${apiBase}/api/studio/v1/workflows/${encodeURIComponent(workflowId)}`,
    { headers: headers() },
  );
  expect(response.status()).toBe(200);
  return await response.json() as {
    id: string;
    status: string;
    version: number;
    viewport: { x: number; y: number; zoom: number };
    nodes: Array<{
      id: string;
      type: string;
      data: {
        config: Record<string, unknown>;
        input_contract_ref?: string | null;
        output_contract_ref?: string | null;
      };
    }>;
  };
}

async function screenshot(page: Page, testInfo: TestInfo, name: string) {
  await testInfo.attach(name, {
    body: await page.screenshot({ animations: "disabled", fullPage: true }),
    contentType: "image/png",
  });
}

test.describe("fresh disposable Studio UI evidence", () => {
  test.skip(!liveEnabled, "requires the isolated disposable service");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "canonical Chromium viewport only");
    expect(apiKey, "isolated service key is required").toBeTruthy();
    expect(authoringWorkflowId, "authoring workflow fixture is required").toBeTruthy();
    expect(configurationWorkflowId, "configuration workflow fixture is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("sidebar navigation and live empty-canvas authoring persist after refresh", async ({
    page,
    request,
  }, testInfo) => {
    coverCriteria(
      testInfo,
      "ui.sidebar-active-route-navigation",
      "ui.empty-canvas-authoring",
      "ui.node-placement",
      "ui.canvas-gestures",
      "ui.undo-redo-refresh",
    );
    const evidence = new BrowserEvidence(page, apiOrigin);

    await page.goto("/console/", { waitUntil: "networkidle" });
    const studioLink = page.getByRole("link", { name: "Studio", exact: true });
    await studioLink.click();
    await expect(page).toHaveURL(/\/console\/studio\/?$/);
    await expect(studioLink).toHaveAttribute("aria-current", "page");
    const runsLink = page.getByRole("link", { name: "Runs", exact: true });
    await runsLink.click();
    await expect(page).toHaveURL(/\/console\/runs\/?$/);
    await expect(runsLink).toHaveAttribute("aria-current", "page");
    await studioLink.click();
    await expect(studioLink).toHaveAttribute("aria-current", "page");
    await screenshot(page, testInfo, "sidebar-active-route-configured");

    await page.goto(
      `/console/studio/edit/?id=${encodeURIComponent(authoringWorkflowId!)}`,
      { waitUntil: "networkidle" },
    );
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
    await expect(page.getByText("Use Add node, choose a type, then place it on the canvas.")).toBeVisible();
    await screenshot(page, testInfo, "empty-canvas-configured");

    await page.getByRole("button", { name: "Add node" }).click();
    await page.getByRole("menuitem", { name: /Agent/ }).click();
    await expect(page.getByRole("status")).toContainText("Place Agent");
    const pane = page.locator(".react-flow__pane");
    const box = await pane.boundingBox();
    expect(box).not.toBeNull();
    await pane.click({ position: { x: Math.round(box!.width * 0.55), y: Math.round(box!.height * 0.55) } });
    await expect(page.locator(".react-flow__node")).toHaveCount(1);

    const viewport = page.locator(".react-flow__viewport");
    const beforeGesture = await viewport.getAttribute("style");
    await pane.hover({ position: { x: Math.round(box!.width / 2), y: Math.round(box!.height / 2) } });
    await page.mouse.wheel(0, 180);
    await expect.poll(() => viewport.getAttribute("style")).not.toBe(beforeGesture);
    const afterWheel = await viewport.getAttribute("style");
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.down({ button: "middle" });
    await page.mouse.move(box!.x + box!.width / 2 + 80, box!.y + box!.height / 2 + 40, { steps: 4 });
    await page.mouse.up({ button: "middle" });
    await expect.poll(() => viewport.getAttribute("style")).not.toBe(afterWheel);

    const undo = page.getByRole("button", { name: "Undo" });
    await expect(undo).toBeEnabled();
    await undo.click();
    await expect(page.locator(".react-flow__node")).toHaveCount(0);
    const redo = page.getByRole("button", { name: "Redo" });
    await expect(redo).toBeEnabled();
    await redo.click();
    await expect(page.locator(".react-flow__node")).toHaveCount(1);
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText(/^Saved /).first()).toBeVisible();

    const saved = await workflow(request, authoringWorkflowId!);
    expect(saved.nodes).toHaveLength(1);
    expect(saved.nodes[0].type).toBe("agent");
    await screenshot(page, testInfo, "authoring-result");
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator(".react-flow__node")).toHaveCount(1);
    const restored = await workflow(request, authoringWorkflowId!);
    expect(restored.nodes).toHaveLength(1);
    await screenshot(page, testInfo, "authoring-refresh-restored");
    await attachSafeJson(testInfo, "authoring-runtime-result", {
      workflow_id: restored.id,
      version: restored.version,
      status: restored.status,
      node_id: restored.nodes[0].id,
      node_type: restored.nodes[0].type,
      sidebar_active_route_navigation: true,
      wheel_zoom_changed_viewport: beforeGesture !== afterWheel,
      middle_drag_changed_viewport: afterWheel !== await viewport.getAttribute("style"),
      undo_removed_node: true,
      redo_restored_node: true,
      refresh_restored_node: true,
    });
    evidence.assertNoFailedApiResponses();
    await evidence.attach(testInfo);
  });

  test("node inspector persists contract connector and approval configuration", async ({
    page,
    request,
  }, testInfo) => {
    coverCriteria(
      testInfo,
      "ui.node-inspector",
      "ui.contract-configuration",
      "ui.connector-configuration",
      "ui.approval-configuration",
    );
    const evidence = new BrowserEvidence(page, apiOrigin);
    await page.goto(
      `/console/studio/edit/?id=${encodeURIComponent(configurationWorkflowId!)}`,
      { waitUntil: "networkidle" },
    );
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();

    await page.locator('.react-flow__node[data-id="agent-config"]').click();
    const agentDialog = page.getByRole("dialog", { name: "Edit Configuration agent" });
    await expect(agentDialog).toBeVisible();
    await expect(agentDialog.getByLabel("Input contract")).toHaveValue("contract://ui-runs-input");
    await expect(agentDialog.getByLabel("Output contract")).toHaveValue("contract://ui-runs-output");
    await screenshot(page, testInfo, "node-contract-inspector-configured");
    await agentDialog.getByLabel("Output contract").selectOption("contract://ui-runs-input");
    await agentDialog.getByRole("button", { name: "Done" }).click();

    await page.locator('.react-flow__node[data-id="retrieval-config"]').click();
    const retrievalDialog = page.getByRole("dialog", { name: "Edit Configured retrieval" });
    await expect(retrievalDialog.getByLabel("Connector")).toHaveValue("ui-runs-memory");
    await retrievalDialog.getByLabel("Top K").fill("4");
    await retrievalDialog.getByRole("button", { name: "Done" }).click();

    await page.locator('.react-flow__node[data-id="approval-config"]').click();
    const approvalDialog = page.getByRole("dialog", { name: "Edit Configured approval" });
    await expect(approvalDialog.getByLabel("SLA timeout (seconds)")).toHaveValue("600");
    await approvalDialog.getByLabel("SLA timeout (seconds)").fill("900");
    await screenshot(page, testInfo, "connector-approval-result");
    await approvalDialog.getByRole("button", { name: "Done" }).click();
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText(/^Saved /).first()).toBeVisible();

    let persisted = await workflow(request, configurationWorkflowId!);
    const byId = Object.fromEntries(persisted.nodes.map((node) => [node.id, node]));
    expect(byId["agent-config"].data.output_contract_ref).toBe("contract://ui-runs-input");
    expect(byId["retrieval-config"].data.config.connector_ref).toBe("ui-runs-memory");
    expect(byId["retrieval-config"].data.config.top_k).toBe(4);
    expect(byId["approval-config"].data.config.sla_timeout_seconds).toBe(900);

    await page.reload({ waitUntil: "networkidle" });
    await page.locator('.react-flow__node[data-id="retrieval-config"]').click();
    const restoredDialog = page.getByRole("dialog", { name: "Edit Configured retrieval" });
    await expect(restoredDialog.getByLabel("Connector")).toHaveValue("ui-runs-memory");
    await expect(restoredDialog.getByLabel("Top K")).toHaveValue("4");
    await screenshot(page, testInfo, "configuration-refresh-restored");
    await restoredDialog.getByRole("button", { name: "Done" }).click();
    persisted = await workflow(request, configurationWorkflowId!);
    await attachSafeJson(testInfo, "configuration-runtime-result", {
      workflow_id: persisted.id,
      status: persisted.status,
      version: persisted.version,
      node_ids: persisted.nodes.map((node) => node.id).sort(),
      output_contract_ref: byId["agent-config"].data.output_contract_ref,
      connector_ref: byId["retrieval-config"].data.config.connector_ref,
      retrieval_top_k: byId["retrieval-config"].data.config.top_k,
      approval_sla_timeout_seconds: byId["approval-config"].data.config.sla_timeout_seconds,
      refresh_restored: true,
    });
    evidence.assertNoFailedApiResponses();
    await evidence.attach(testInfo);
  });

  test("preflight issue focuses the exact failing node without provider traffic", async ({
    page,
  }, testInfo) => {
    coverCriteria(testInfo, "ui.preflight-error-focus");
    const evidence = new BrowserEvidence(page, apiOrigin);
    let providerCalls = 0;
    page.on("request", (request) => {
      if (request.url().includes("verify-provider")) providerCalls += 1;
    });
    await page.goto(
      `/console/studio/edit/?id=${encodeURIComponent(configurationWorkflowId!)}`,
      { waitUntil: "networkidle" },
    );
    const viewport = page.locator(".react-flow__viewport");
    const before = await viewport.getAttribute("style");
    await page.getByRole("button", { name: "Run preflight" }).click();
    await expect(page.getByText(/Can't publish yet/)).toBeVisible();
    await expect(page.getByText("agent model provider is required")).toBeVisible();
    await screenshot(page, testInfo, "preflight-error-configured");
    await page.getByRole("button", { name: "agent-config →" }).click();
    await expect(page.locator('.react-flow__node[data-id="agent-config"]')).toHaveClass(/selected/);
    await expect.poll(() => viewport.getAttribute("style")).not.toBe(before);
    const afterFocus = await viewport.getAttribute("style");
    expect(providerCalls).toBe(0);
    await screenshot(page, testInfo, "preflight-focused-result");
    await page.reload({ waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Run preflight" }).click();
    await expect(page.getByText("agent model provider is required")).toBeVisible();
    await screenshot(page, testInfo, "preflight-refresh-restored");
    await attachSafeJson(testInfo, "preflight-runtime-result", {
      issue_code: "invalid_node_attachment",
      focused_node_id: "agent-config",
      viewport_changed: before !== afterFocus,
      provider_calls_performed: providerCalls,
      refresh_restored_issue: true,
    });
    evidence.assertNoFailedApiResponses();
    await evidence.attach(testInfo);
  });
});
