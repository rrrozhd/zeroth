import { expect, test } from "@playwright/test";

import { configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;

test("manifest detail is inspectable and linked to retained run evidence", async ({ page }, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  coverCriteria(testInfo, "manifests.detail", "manifests.run-linkage");
  await configurePage(page, apiBase, tenant, apiKey!);

  await page.goto("/console/metrics/", { waitUntil: "domcontentloaded" });
  const inspect = page.getByRole("button", { name: /^Inspect evaluation:/ }).first();
  await expect(inspect).toBeVisible();
  await inspect.click();

  await expect(page.getByText("content hash", { exact: true })).toBeVisible();
  await expect(page.getByText("Commands, source, environment, and secret bindings stay hidden"))
    .toBeVisible();
  await expect(page.getByText("Input schema", { exact: true })).toBeVisible();
  await testInfo.attach("manifest-detail-success", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
});

test("registered executable manifest is inspectable from Studio", async ({ page }, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  coverCriteria(testInfo, "manifests.studio-detail", "manifests.safe-projection");
  await configurePage(page, apiBase, tenant, apiKey!);

  await page.goto(
    "/console/studio/edit/?id=45417e22-b2ae-4b7e-948f-3869290ed031",
    { waitUntil: "networkidle" },
  );
  await page.locator('.react-flow__node[data-id="profile"]').click();
  await page.getByText("Inspect registered manifest", { exact: true }).click();

  await expect(page.getByText("content hash", { exact: true })).toBeVisible();
  await expect(page.getByText("Commands, source, environment, and secret bindings stay hidden"))
    .toBeVisible();
  await expect(page.getByText("Input schema", { exact: true })).toBeVisible();
  await testInfo.attach("studio-manifest-detail-success", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
});
