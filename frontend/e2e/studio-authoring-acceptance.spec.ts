import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { attachSafeJson, coverCriteria } from "./support/live-evaluation";

type StudioNode = {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
};

type Draft = {
  id: string;
  name: string;
  status: string;
  version: number;
  updated_at: string;
  entry_step: string | null;
  viewport: { x: number; y: number; zoom: number };
  nodes: StudioNode[];
  edges: object[];
};

const workflowId = "ui-evidence";

function draft(nodes: StudioNode[] = []): Draft {
  return {
    id: workflowId,
    name: "UI evidence draft",
    status: "draft",
    version: 1,
    updated_at: "2026-08-22T00:00:00Z",
    entry_step: null,
    viewport: { x: 0, y: 0, zoom: 1 },
    nodes,
    edges: [],
  };
}

async function mockStudio(page: Page, initial: Draft) {
  let current = structuredClone(initial);
  const updates: object[] = [];
  const requests: string[] = [];
  await page.route("**/api/studio/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    requests.push(`${request.method()} ${path}`);
    const json = (body: object) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/studio/v1/node-types") {
      return json([
        { type: "entrypoint", label: "Entrypoint", category: "core", ports: [{ id: "output-data", type: "data", direction: "output", label: "Output" }] },
        { type: "agent", label: "Agent", category: "core", ports: [
          { id: "input-data", type: "data", direction: "input", label: "Input" },
          { id: "output-data", type: "data", direction: "output", label: "Output" },
          { id: "tools", type: "tool", direction: "output", label: "Tools" },
        ] },
      ]);
    }
    if (path === "/api/studio/v1/contracts") {
      return json([
        { name: "contract://question", version: 1, json_schema: { type: "object" } },
        { name: "contract://answer", version: 1, json_schema: { type: "object" } },
      ]);
    }
    if (path === `/api/studio/v1/workflows/${workflowId}/preflight`) {
      return json({
        workflow_id: workflowId,
        version: 1,
        ready: false,
        checks: ["agent.provider"],
        issues: [{
          code: "agent_provider_missing",
          message: "Configure a model provider before publishing.",
          severity: "error",
          node_id: "agent-1",
          edge_id: null,
        }],
      });
    }
    if (path === `/api/studio/v1/workflows/${workflowId}` && request.method() === "PUT") {
      const body = request.postDataJSON() as Partial<Draft>;
      updates.push(body);
      current = {
        ...current,
        name: body.name ?? current.name,
        entry_step: body.entry_step ?? null,
        nodes: (body.nodes ?? current.nodes) as StudioNode[],
        edges: body.edges ?? current.edges,
        viewport: body.viewport ?? current.viewport,
      };
      return json(current);
    }
    if (path === `/api/studio/v1/workflows/${workflowId}`) return json(current);
    if (path === "/api/studio/v1/workflows") {
      return json([{ id: current.id, name: current.name, status: current.status, version: current.version, updated_at: current.updated_at }]);
    }
    return json([]);
  });
  await page.route("**/v1/connectors", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/v1/manifests", (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  return { updates, requests, current: () => structuredClone(current) };
}

async function expectEditorLoaded(page: Page, requests: string[]) {
  await expect(page.getByText("Loading graph…"), JSON.stringify(requests)).toBeHidden({ timeout: 8_000 });
  await expect(page.getByRole("button", { name: "Add node" }), JSON.stringify(requests)).toBeVisible();
}

async function attachInteractionEvidence(page: Page, testInfo: TestInfo, name: string, facts: object) {
  const screenshot = await page.screenshot({ animations: "disabled", fullPage: true });
  await testInfo.attach(name, { body: screenshot, contentType: "image/png" });
  await attachSafeJson(testInfo, `${name}-assertions`, facts);
}

test.beforeEach(async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-1440", "authoring assertions run once at the canonical desktop viewport");
  await page.addInitScript(() => {
    window.localStorage.setItem("zeroth.apiBase", "");
    window.localStorage.setItem("zeroth.sessionActive", "1");
    window.localStorage.setItem("zeroth.tenant", "evaluation-studio-v1");
  });
});

test("empty canvas placement, gestures, undo, redo, and refresh preserve authored state", async ({ page }, testInfo) => {
  coverCriteria(
    testInfo,
    "ui.empty-canvas-authoring",
    "ui.node-placement",
    "ui.canvas-gestures",
    "ui.undo-redo-refresh",
  );
  const fixture = await mockStudio(page, draft());
  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
  await expectEditorLoaded(page, fixture.requests);

  await expect(page.getByText("Use Add node, choose a type, then place it on the canvas.")).toBeVisible();
  await page.getByRole("button", { name: "Add node" }).click();
  await page.getByRole("menuitem", { name: /Agent/ }).click();
  await expect(page.getByRole("status")).toContainText("Place Agent");
  const pane = page.locator(".react-flow__pane");
  const box = await pane.boundingBox();
  expect(box).not.toBeNull();
  await pane.click({ position: { x: Math.round(box!.width * 0.55), y: Math.round(box!.height * 0.55) } });
  await expect(page.locator(".react-flow__node")).toHaveCount(1);
  await expect(page.getByText("Use Add node, choose a type, then place it on the canvas.")).toHaveCount(0);

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
  await expect.poll(() => fixture.updates.length).toBeGreaterThan(0);
  await page.reload({ waitUntil: "networkidle" });
  await expect(page.locator(".react-flow__node")).toHaveCount(1);

  await attachInteractionEvidence(page, testInfo, "studio-authoring", {
    empty_state_observed: true,
    placed_nodes: fixture.current().nodes.length,
    viewport_changed_by_wheel: beforeGesture !== afterWheel,
    viewport_changed_by_middle_drag: afterWheel !== await viewport.getAttribute("style"),
    undo_removed_node: true,
    redo_restored_node: true,
    refresh_restored_saved_node: true,
    persisted_update_count: fixture.updates.length,
  });
});

test("agent inspector persists provider and contract configuration", async ({ page }, testInfo) => {
  coverCriteria(testInfo, "ui.provider-configuration", "ui.contract-configuration");
  const fixture = await mockStudio(page, draft([{
    id: "agent-1",
    type: "agent",
    position: { x: 220, y: 180 },
    data: { label: "Evidence agent", config: {}, input_contract_ref: null, output_contract_ref: null },
  }]));
  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
  await expectEditorLoaded(page, fixture.requests);
  await page.locator('.react-flow__node[data-id="agent-1"]').dblclick();
  const dialog = page.getByRole("dialog", { name: "Edit Evidence agent" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Model provider").fill("openai/gpt-4o-mini");
  await dialog.getByLabel("Input contract").selectOption("contract://question");
  await dialog.getByLabel("Output contract").selectOption("contract://answer");
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect.poll(() => fixture.updates.length).toBeGreaterThan(0);
  const persisted = fixture.current().nodes[0].data;
  expect((persisted.config as Record<string, unknown>).model_provider).toBe("openai/gpt-4o-mini");
  expect(persisted.input_contract_ref).toBe("contract://question");
  expect(persisted.output_contract_ref).toBe("contract://answer");
  await attachInteractionEvidence(page, testInfo, "studio-provider-contracts", {
    node_id: "agent-1",
    provider: (persisted.config as Record<string, unknown>).model_provider,
    input_contract_ref: persisted.input_contract_ref,
    output_contract_ref: persisted.output_contract_ref,
    persisted_update_count: fixture.updates.length,
  });
});

test("preflight issue action focuses the exact failing node without a provider call", async ({ page }, testInfo) => {
  coverCriteria(testInfo, "ui.preflight-error-focus");
  const fixture = await mockStudio(page, draft([{
    id: "agent-1",
    type: "agent",
    position: { x: 1600, y: 900 },
    data: { label: "Unconfigured agent", config: {}, input_contract_ref: null, output_contract_ref: null },
  }]));
  let providerCalls = 0;
  await page.route("**/verify-provider", async (route) => {
    providerCalls += 1;
    await route.abort();
  });
  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
  await expectEditorLoaded(page, fixture.requests);
  const viewport = page.locator(".react-flow__viewport");
  const before = await viewport.getAttribute("style");
  await page.getByRole("button", { name: "Run preflight" }).click();
  await expect(page.getByText("Can't publish yet (1 error)")).toBeVisible();
  await expect(page.getByText("Configure a model provider before publishing.")).toBeVisible();
  await page.getByRole("button", { name: "agent-1 →" }).click();
  const node = page.locator('.react-flow__node[data-id="agent-1"]');
  await expect(node).toHaveClass(/selected/);
  await expect.poll(() => viewport.getAttribute("style")).not.toBe(before);
  expect(providerCalls).toBe(0);
  await attachInteractionEvidence(page, testInfo, "studio-preflight-focus", {
    issue_code: "agent_provider_missing",
    focused_node_id: "agent-1",
    selected: true,
    viewport_changed: before !== await viewport.getAttribute("style"),
    provider_calls: providerCalls,
  });
});
