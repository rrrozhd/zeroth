import { expect, test } from "@playwright/test";

import {
  assertAccessibility,
  assertDocumentLoaded,
  assertKeyboardFocus,
  assertMinimumTargets,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
  workflowFixture,
  type WorkflowFixture,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8120";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;

const workflows = [1, 2, 3] as const;

function requireFixture(index: 1 | 2 | 3): WorkflowFixture {
  const fixture = workflowFixture(index);
  expect(fixture, `workflow ${index} fixture environment is incomplete`).not.toBeNull();
  return fixture!;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test.describe("evidence-first live workflow journeys", () => {
  test.skip(!liveEnabled, "requires ZEROTH_EVALUATION_LIVE=1 and an isolated evaluation service fixture");

  test.beforeEach(async ({ page, request }) => {
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required for the isolated fixture").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);

    let health;
    try {
      health = await request.get(`${apiBase}/health`, {
        headers: { "X-API-Key": apiKey!, "X-Tenant-ID": tenant },
        timeout: 5_000,
      });
    } catch (error) {
      throw new Error(`evaluation service fixture unavailable at ${apiOrigin}`, { cause: error });
    }
    expect(health.status(), "evaluation service fixture health was not 2xx").toBeGreaterThanOrEqual(200);
    expect(health.status(), "evaluation service fixture health was not 2xx").toBeLessThan(300);
  });

  for (const workflowIndex of workflows) {
    test(`workflow ${workflowIndex} template authors an inspectable draft`, async ({ page }, testInfo) => {
      coverCriteria(testInfo, "ui.node-menu", "ui.keyboard-shortcuts");
      test.skip(testInfo.project.name !== "desktop-1440", "authoring mutation runs once; responsive checks use the fixture journeys");
      test.skip(process.env.ZEROTH_EVALUATION_AUTHORING_MUTATIONS !== "1", "requires a disposable Studio authoring fixture");
      const fixture = requireFixture(workflowIndex);
      const evidence = new BrowserEvidence(page, apiOrigin);
      await assertDocumentLoaded(page, "/studio");
      // Workflow rows are links, not buttons. Address the row by its stable
      // workflow identity so renaming a draft cannot make the authoring path
      // unreachable or silently select a same-named tenant fixture.
      await page.getByRole("link", { name: new RegExp(escapeRegExp(fixture.id)) }).click();
      await expect(page).toHaveURL(/\/studio\/edit\/?\?id=/);
      await expect(page.getByLabel("Workflow name")).toHaveValue(fixture.expectedName);
      await expect(page.getByLabel("Workflow graph editor")).toBeVisible();

      await page.getByRole("button", { name: "Add node", exact: true }).click();
      const menu = page.getByRole("menu", { name: "Node types" });
      await expect(menu).toBeVisible();
      await expect(menu.getByRole("menuitem").first()).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(menu).toBeHidden();
      await page.keyboard.press(process.platform === "darwin" ? "Meta+s" : "Control+s");
      evidence.assertNoFailedApiResponses();
      await evidence.attach(testInfo);
      await page.screenshot({ path: testInfo.outputPath(`workflow-${workflowIndex}-authored.png`), animations: "disabled", fullPage: true });
    });

    test(`workflow ${workflowIndex} configured graph is inspectable and accessible`, async ({ page }, testInfo) => {
      coverCriteria(
        testInfo,
        "ui.node-inspector",
        "ui.focus-visible-order",
        "ui.target-size",
        "ui.modal-focus",
        "ui.reduced-motion",
        "ui.axe-wcag22-aa",
      );
      if (workflowIndex === 1) {
        coverCriteria(testInfo, "ui.connector-configuration", "ui.loop-configuration");
      } else if (workflowIndex === 2) {
        coverCriteria(testInfo, "ui.batch-join-configuration");
      } else {
        coverCriteria(testInfo, "ui.approval-configuration");
      }
      const fixture = requireFixture(workflowIndex);
      const evidence = new BrowserEvidence(page, apiOrigin);
      await page.emulateMedia({ reducedMotion: "reduce" });

      await assertDocumentLoaded(page, `/studio/edit?id=${encodeURIComponent(fixture.id)}`);
      await expect(page.getByLabel("Workflow name")).toHaveValue(fixture.expectedName);
      await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
      await page.locator(`.react-flow__node[data-id="${fixture.inspectNode}"]`).click();
      const dialog = page.getByRole("dialog", { name: `Edit ${fixture.inspectNode}` });
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute("aria-modal", "true");
      await expect(dialog.locator(":focus")).toHaveCount(1);
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);

      if (workflowIndex === 1) {
        await expect(dialog.getByLabel("Connector")).toHaveValue(
          process.env.ZEROTH_EVALUATION_WORKFLOW1_CONNECTOR_REF ?? "eval_chroma_v1",
        );
      } else if (workflowIndex === 2) {
        await dialog.getByRole("button", { name: "Execution" }).click();
        await expect(dialog.getByLabel("Maximum concurrency")).toHaveValue(
          process.env.ZEROTH_EVALUATION_WORKFLOW2_MAX_CONCURRENCY ?? "4",
        );
        await expect(dialog.getByLabel("Batch size")).toHaveValue(
          process.env.ZEROTH_EVALUATION_WORKFLOW2_BATCH_SIZE ?? "4",
        );
      } else {
        await expect(dialog.getByLabel("SLA timeout (seconds)")).not.toHaveValue("");
      }

      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      if (workflowIndex === 2) {
        await page.locator(`.react-flow__node[data-id="${fixture.childNode!}"]`).click();
        const childDialog = page.getByRole("dialog", { name: `Edit ${fixture.childNode}` });
        await expect(childDialog.getByLabel("Graph ref")).toHaveValue(
          process.env.ZEROTH_EVALUATION_WORKFLOW2_CHILD_GRAPH_REF ?? "template-iterative-research",
        );
        await page.keyboard.press("Escape");
      }
      if (workflowIndex === 3) {
        const actionNode = process.env.ZEROTH_EVALUATION_WORKFLOW3_ACTION_NODE;
        const manifestRef = process.env.ZEROTH_EVALUATION_WORKFLOW3_ACTION_MANIFEST_REF;
        expect(actionNode, "workflow 3 action-node fixture is required").toBeTruthy();
        expect(manifestRef, "workflow 3 evaluation action manifest fixture is required").toBeTruthy();
        await page.locator(`.react-flow__node[data-id="${actionNode!}"]`).click();
        const actionDialog = page.getByRole("dialog", { name: `Edit ${actionNode}` });
        await expect(actionDialog).toBeVisible();
        await expect(actionDialog.getByLabel("Manifest ref")).toHaveValue(manifestRef!);
        await page.keyboard.press("Escape");
      }
      await assertKeyboardFocus(page, testInfo);
      await assertMinimumTargets(page, testInfo);
      await assertAccessibility(page, testInfo);
      evidence.assertNoFailedApiResponses();
      await evidence.attach(testInfo);
      await page.screenshot({ path: testInfo.outputPath(`workflow-${workflowIndex}-configured.png`), animations: "disabled", fullPage: true });
    });

    test(`workflow ${workflowIndex} publish and deploy lifecycle`, async ({ page }, testInfo) => {
      coverCriteria(testInfo, "ui.publish-deploy-run");
      test.skip(process.env.ZEROTH_EVALUATION_LIFECYCLE_MUTATIONS !== "1", "requires a disposable draft fixture and lifecycle mutation authorization");
      const fixture = requireFixture(workflowIndex);
      const evidence = new BrowserEvidence(page, apiOrigin);
      await assertDocumentLoaded(page, `/studio/edit?id=${encodeURIComponent(fixture.id)}`);

      const publish = page.getByRole("button", { name: "Publish", exact: true }).first();
      if (await publish.isVisible()) {
        await page.getByRole("button", { name: "Run preflight", exact: true }).first().click();
        await expect(page.getByLabel("Workflow verification states")).toContainText("Preflight passed");
        await publish.click();
        await expect(page.getByLabel("Workflow verification states")).toContainText("Published");
      }

      await page.getByRole("button", { name: "Deploy", exact: true }).first().click();
      const dialog = page.getByRole("dialog", { name: "Deploy workflow" });
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute("aria-modal", "true");
      await dialog.getByLabel("Deployment ref").fill(fixture.deploymentRef);
      await dialog.getByRole("button", { name: "Deploy", exact: true }).click();
      await expect(dialog).toContainText("Created");
      await attachSafeJson(testInfo, "lifecycle-result", {
        workflow_index: workflowIndex,
        graph_version_ref: fixture.expectedGraphVersion,
        deployment_ref: fixture.deploymentRef,
        cleanup_state: "service_restart_required",
      });
      evidence.assertNoFailedApiResponses();
      await evidence.attach(testInfo);
    });

    for (let repetition = 1; repetition <= 3; repetition += 1) {
      test(`workflow ${workflowIndex} served happy path repetition ${repetition}`, async ({ page, request }, testInfo) => {
        coverCriteria(testInfo, "ui.publish-deploy-run");
        test.skip(
          process.env.ZEROTH_EVALUATION_ALLOW_PROVIDER_RUNS !== "I_ACKNOWLEDGE_BOUNDED_PROVIDER_COST",
          "provider-backed runs require the exact bounded-cost acknowledgement",
        );
        const fixture = requireFixture(workflowIndex);
        const evidence = new BrowserEvidence(page, apiOrigin);
        const health = await request.get(`${apiBase}/health`, {
          headers: { "X-API-Key": apiKey!, "X-Tenant-ID": tenant },
          timeout: 5_000,
        });
        expect(health.status()).toBeGreaterThanOrEqual(200);
        expect(health.status()).toBeLessThan(300);
        const healthBody = await health.json() as { graph_version_ref?: string; deployment_ref?: string };
        expect(healthBody.graph_version_ref, "served graph version must exactly match the fixture").toBe(fixture.expectedGraphVersion);
        expect(healthBody.deployment_ref, "served deployment must exactly match the fixture").toBe(fixture.deploymentRef);
        await assertDocumentLoaded(page, `/studio/edit?id=${encodeURIComponent(fixture.id)}`);
        await page.getByRole("button", { name: "Run", exact: true }).first().click();
        const input = page.getByLabel("Input payload (JSON)");
        await expect(input).toBeVisible();
        await input.fill(fixture.inputPayload);
        await page.getByRole("button", { name: "Run", exact: true }).last().click();
        await expect(page.getByText("completed", { exact: true }).first()).toBeVisible({ timeout: 120_000 });
        await attachSafeJson(testInfo, "run-result", {
          workflow_index: workflowIndex,
          repetition,
          expected_graph_version_ref: fixture.expectedGraphVersion,
        });
        evidence.assertNoFailedApiResponses();
        await evidence.attach(testInfo);
      });
    }
  }

  for (const decision of ["Approve", "Reject"] as const) {
    test(`workflow 3 ${decision.toLowerCase()} survives refresh before resolution`, async ({ page }, testInfo) => {
      test.skip(process.env.ZEROTH_EVALUATION_APPROVAL_MUTATIONS !== "1", "requires a pending disposable approval fixture");
      const nodeId = process.env[`ZEROTH_EVALUATION_${decision.toUpperCase()}_NODE_ID`];
      expect(nodeId, `${decision} approval node fixture is required`).toBeTruthy();
      const evidence = new BrowserEvidence(page, apiOrigin);

      await assertDocumentLoaded(page, "/approvals");
      await expect(page.getByText(nodeId!, { exact: true })).toBeVisible();
      await page.reload({ waitUntil: "networkidle" });
      await expect(page.getByText(nodeId!, { exact: true })).toBeVisible();
      await page.getByText(nodeId!, { exact: true }).locator("xpath=ancestor::*[.//button]").first()
        .getByRole("button", { name: decision, exact: true }).click();
      await expect(page.getByText(decision === "Approve" ? "approved" : "rejected", { exact: true }).first()).toBeVisible();
      await attachSafeJson(testInfo, "approval-resolution", { decision: decision.toLowerCase(), node_id: nodeId });
      evidence.assertNoFailedApiResponses();
      await evidence.attach(testInfo);
    });
  }
});
