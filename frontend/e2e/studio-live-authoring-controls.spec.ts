import { expect, test } from "@playwright/test";

import {
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = process.env.ZEROTH_EVALUATION_AUTHORING_WORKFLOW_ID;
const workflowName = process.env.ZEROTH_EVALUATION_AUTHORING_WORKFLOW_NAME;

test.describe("live Studio authoring controls", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test("node menu and keyboard save survive refresh on a persistent draft", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint uses the canonical viewport");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    expect(workflowId, "ZEROTH_EVALUATION_AUTHORING_WORKFLOW_ID is required").toBeTruthy();
    expect(workflowName, "ZEROTH_EVALUATION_AUTHORING_WORKFLOW_NAME is required").toBeTruthy();
    coverCriteria(testInfo, "ui.node-menu", "ui.keyboard-shortcuts");
    await configurePage(page, apiBase, tenant, apiKey!);
    const evidence = new BrowserEvidence(page, apiOrigin);

    await page.goto("/console/studio/", { waitUntil: "networkidle" });
    const workflowLink = page.getByRole("link", { name: new RegExp(workflowId!) }).first();
    await expect(workflowLink).toBeVisible();
    await workflowLink.click();
    await expect(page).toHaveURL(new RegExp(`/studio/edit/\\?id=${workflowId}$`));
    await expect(page.getByLabel("Workflow name")).toHaveValue(workflowName!);
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();

    const addNode = page.getByRole("button", { name: "Add node", exact: true });
    await expect(addNode).toBeEnabled();
    await addNode.click();
    const menu = page.getByRole("menu", { name: "Node types" });
    await expect(menu).toBeVisible();
    await expect(menu.getByRole("menuitem").first()).toBeVisible();
    await testInfo.attach("node-menu-configured", {
      body: await page.screenshot({ animations: "disabled", fullPage: true }),
      contentType: "image/png",
    });

    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    await page.keyboard.press(process.platform === "darwin" ? "Meta+s" : "Control+s");
    await expect(page.getByText(/^Saved /).first()).toBeVisible();

    const persisted = await request.get(`${apiBase}/api/studio/v1/workflows/${encodeURIComponent(workflowId!)}`, {
      headers: { "X-API-Key": apiKey!, "X-Tenant-ID": tenant },
    });
    expect(persisted.status()).toBe(200);
    const persistedBody = await persisted.json() as {
      id: string;
      name: string;
      status: string;
      version: number;
    };
    expect(persistedBody).toMatchObject({
      id: workflowId,
      name: workflowName,
      status: "draft",
    });
    await attachSafeJson(testInfo, "keyboard-save-runtime", {
      workflow_id: persistedBody.id,
      workflow_name: persistedBody.name,
      status: persistedBody.status,
      version: persistedBody.version,
      save_shortcut: process.platform === "darwin" ? "Meta+s" : "Control+s",
      node_menu_opened: true,
    });
    await testInfo.attach("keyboard-save-result", {
      body: await page.screenshot({ animations: "disabled", fullPage: true }),
      contentType: "image/png",
    });

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByLabel("Workflow name")).toHaveValue(workflowName!);
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
    await testInfo.attach("keyboard-save-refresh-restored", {
      body: await page.screenshot({ animations: "disabled", fullPage: true }),
      contentType: "image/png",
    });
    evidence.assertNoFailedApiResponses();
    await evidence.attach(testInfo);
  });
});
