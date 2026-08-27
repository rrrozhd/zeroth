import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { attachSafeJson, configurePage } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = process.env.ZEROTH_EVALUATION_INVENTORY_WORKFLOW_ID
  ?? "evaluation-studio-v1-governed-remediation";
const activityNode = process.env.ZEROTH_EVALUATION_INVENTORY_ACTIVITY_NODE
  ?? "synthetic-action";

const interactiveSelector = [
  "button:not([aria-label='Open Next.js Dev Tools'])",
  "a[href]",
  "input:not([type=hidden])",
  "select",
  "textarea",
  "summary",
  "[contenteditable=true]",
  "[role=button]",
  "[role=checkbox]",
  "[role=combobox]",
  "[role=radio]",
  "[role=slider]",
  "[role=switch]",
  "[role=textbox]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

async function assertUniqueEvidenceIdentities(page: Page, testInfo: TestInfo, checkpoint: string) {
  await expect.poll(async () => page.locator("html").getAttribute("data-evidence-identity-errors"))
    .toBe("[]");
  const inventory = await page.locator(interactiveSelector).evaluateAll((controls) => {
    const rows = controls.map((control) => {
      const element = control as HTMLElement;
      return {
        evidence_id: element.dataset.evidenceId ?? null,
        tag: element.tagName.toLowerCase(),
        role: element.getAttribute("role"),
        label: element.getAttribute("aria-label") ?? element.textContent?.trim().slice(0, 120),
        class_name: element.className,
      };
    });
    const identities = rows.map((row) => row.evidence_id);
    return {
      count: identities.length,
      missing: rows.filter((row) => row.evidence_id === null),
      duplicate_count: identities.length - new Set(identities).size,
    };
  });
  expect(inventory.count).toBeGreaterThan(0);
  expect(inventory.missing, JSON.stringify(inventory.missing)).toEqual([]);
  expect(inventory.duplicate_count).toBe(0);
  await attachSafeJson(testInfo, `${checkpoint}-identity-inventory`, inventory);
}

test.describe("dynamic Studio evidence identities", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint uses the canonical viewport");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("state labels recompute and repeated workflow rows remain unique", async ({ page }, testInfo) => {
    await page.goto("/console/studio/", { waitUntil: "networkidle" });
    const refresh = page.getByRole("button", { name: "Refresh", exact: true });
    await expect(refresh).toHaveAttribute("data-evidence-id", "studio.button.refresh");
    await refresh.click();
    await expect(refresh).toHaveText("Refresh");
    await expect(refresh).toHaveAttribute("data-evidence-id", "studio.button.refresh");
    await assertUniqueEvidenceIdentities(page, testInfo, "studio-list-restored");
    await testInfo.attach("studio-list-restored", {
      body: await page.screenshot({ animations: "disabled", fullPage: true }),
      contentType: "image/png",
    });
  });

  test("History and Activity expansions keep resource-qualified identities", async ({ page }, testInfo) => {
    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
    await assertUniqueEvidenceIdentities(page, testInfo, "studio-editor-base");

    await page.locator("button:visible", { hasText: "History" }).click();
    const history = page.getByRole("dialog", { name: "Version history" });
    await expect(history).toBeVisible();
    await history.getByLabel("Left version").selectOption("1");
    await expect(history.getByText("before / after").first()).toBeVisible();
    await assertUniqueEvidenceIdentities(page, testInfo, "studio-history-open");
    await testInfo.attach("studio-history-identities", {
      body: await page.screenshot({ animations: "disabled", fullPage: true }),
      contentType: "image/png",
    });
    await history.getByRole("button", { name: "Close" }).click();

    await page.locator(`.react-flow__node[data-id="${activityNode}"]`).click();
    const inspector = page.getByRole("dialog", { name: /Edit / });
    await inspector.getByRole("button", { name: "Activity", exact: true }).click();
    await expect(inspector.getByText(/executions? shown/)).toBeVisible();
    await expect(inspector.getByText("Output snapshot").first()).toBeVisible();
    await assertUniqueEvidenceIdentities(page, testInfo, "studio-activity-open");
    await testInfo.attach("studio-activity-identities", {
      body: await page.screenshot({ animations: "disabled", fullPage: true }),
      contentType: "image/png",
    });
  });
});
