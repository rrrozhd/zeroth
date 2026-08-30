import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { configurePage } from "./support/live-evaluation";

const workflowId =
  process.env.ZEROTH_EVALUATION_IF_WORKFLOW_ID ??
  "evaluation-studio-v1-governed-remediation";

test.beforeEach(async ({ page }) => {
  const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
  if (!apiKey) return;
  await configurePage(
    page,
    process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122",
    process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1",
    apiKey,
  );
});

test("explicit If routing and Run modal are durable in the real Studio", async ({ page }, testInfo) => {
  const failedResponses: Array<{ status: number; url: string }> = [];
  const consoleErrors: string[] = [];
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() >= 400 && (url.includes("/v1/") || url.includes("/api/studio/"))) {
      failedResponses.push({ status: response.status(), url: new URL(url).pathname });
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId)}`, {
    waitUntil: "networkidle",
  });

  const decision = page.getByRole("group", { name: "Workflow node evaluation-route" });
  await expect(decision).toBeVisible();
  await expect(decision.getByText("Route remediation")).toBeVisible();
  await expect(decision.getByText("payload.evaluation_behavior == 'cancel_after_approval'")).toBeVisible();
  await expect(decision.getByRole("button", { name: "True" })).toBeVisible();
  await expect(decision.getByRole("button", { name: "False" })).toBeVisible();
  await expect(page.locator(".studio-edge-label")).toHaveCount(0);

  await page.getByRole("button", { name: "Run", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Run workflow" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect(dialog.getByRole("button", { name: "Close run dialog" })).toBeFocused();
  const backdrop = await page.locator(".studio-dialog-backdrop").evaluate((element) => {
    const styles = window.getComputedStyle(element);
    return styles.backdropFilter || styles.getPropertyValue("-webkit-backdrop-filter");
  });
  expect(backdrop).not.toBe("none");

  const violations = (await new AxeBuilder({ page }).include("[role=dialog]").analyze()).violations;
  expect(violations.map((violation) => violation.id)).toEqual([]);
  await testInfo.attach("if-routing-run-modal", {
    body: await page.screenshot({ animations: "disabled" }),
    contentType: "image/png",
  });

  const width = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(width.scroll).toBeLessThanOrEqual(width.client + 1);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  expect(failedResponses).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
