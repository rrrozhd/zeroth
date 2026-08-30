import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";

import { sanitizeUrl, summarizeRequest, summarizeResponse } from "./support/sanitized-network";
import { configurePage, coverCriteria } from "./support/live-evaluation";

const incidentLoopWorkflowId =
  process.env.ZEROTH_EVALUATION_INCIDENT_LOOP_WORKFLOW_ID ??
  "da5da69b-1086-4cfe-8090-424a0118b88c";

const routes = [
  { id: "overview", target: "/console/" },
  { id: "approvals", target: "/console/approvals/" },
  { id: "artifacts", target: "/console/artifacts/" },
  { id: "audit", target: "/console/audit/" },
  { id: "connectors", target: "/console/connectors/" },
  { id: "cost", target: "/console/cost/" },
  { id: "deployments", target: "/console/deployments/" },
  { id: "guide", target: "/console/guide/" },
  { id: "metrics", target: "/console/metrics/" },
  { id: "regulus", target: "/console/regulus/" },
  { id: "regulus-capabilities", target: "/console/regulus/capabilities/" },
  { id: "regulus-costing", target: "/console/regulus/costing/" },
  { id: "regulus-enforcement", target: "/console/regulus/enforcement/" },
  { id: "regulus-reconciliation", target: "/console/regulus/reconciliation/" },
  { id: "retention", target: "/console/retention/" },
  { id: "rightsizing", target: "/console/rightsizing/" },
  { id: "runs", target: "/console/runs/" },
  { id: "studio", target: "/console/studio/" },
  {
    id: "studio-edit",
    target: `/console/studio/edit/?id=${incidentLoopWorkflowId}`,
  },
  { id: "templates", target: "/console/templates/" },
  { id: "webhooks", target: "/console/webhooks/" },
] as const;

type FocusEvidence = {
  tag: string;
  role: string | null;
  focus_visible: boolean;
} | null;

function digest(value: string) {
  return createHash("sha256").update(value).digest("hex");
}

test.beforeEach(async ({ page }) => {
  const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
  if (!apiKey) return;
  await configurePage(
    page,
    process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8120",
    process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1",
    apiKey,
  );
});

for (const route of routes) {
  test(`${route.id} has durable, credential-safe UI evidence`, async ({ page }, testInfo) => {
    const viewportCriterion = {
      "desktop-1440": "ui.viewport-1440x900",
      "webkit-1440": "ui.viewport-1440x900",
      "desktop-1280": "ui.viewport-1280x800",
      "tablet-768": "ui.viewport-768x1024",
      "mobile-390": "ui.viewport-390x844",
    }[testInfo.project.name];
    if (!viewportCriterion) throw new Error("unrecognized evaluation viewport project");
    coverCriteria(
      testInfo,
      viewportCriterion,
      "ui.operational-surfaces",
      "ui.focus-visible-order",
      "ui.reduced-motion",
      "ui.axe-wcag22-aa",
      "stop.no-indefinite-loading",
    );
    await page.emulateMedia({ reducedMotion: "reduce" });
    const requests: object[] = [];
    const responses: object[] = [];
    const consoleEvents: object[] = [];
    const consoleErrors: object[] = [];
    const failedResponses: object[] = [];
    page.on("request", (request) => {
      requests.push(
        summarizeRequest({
          method: request.method(),
          url: request.url(),
          resourceType: request.resourceType(),
          postData: request.postData(),
        }),
      );
    });
    page.on("response", (response) => {
      const summary = summarizeResponse({
        url: response.url(),
        status: response.status(),
        resourceType: response.request().resourceType(),
      });
      responses.push(summary);
      if (response.status() >= 400) failedResponses.push(summary);
    });
    page.on("console", (message) => {
      const text = message.text();
      const summary = {
        type: message.type(),
        message_bytes: Buffer.byteLength(text),
        message_sha256: digest(text),
        url: message.location().url ? sanitizeUrl(message.location().url) : null,
      };
      consoleEvents.push(summary);
      if (message.type() === "error") consoleErrors.push(summary);
    });

    const response = await page.goto(route.target, { waitUntil: "networkidle" });
    expect(response?.status()).toBeGreaterThanOrEqual(200);
    expect(response?.status()).toBeLessThan(400);
    await expect(page.locator("main")).toBeVisible();
    const secretVisible = await page.evaluate(() => {
      const values = Array.from(document.querySelectorAll("input, textarea"), (element) =>
        (element as HTMLInputElement | HTMLTextAreaElement).value,
      );
      const candidate = [window.location.href, document.body.innerText, ...values].join("\n");
      return /\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bAuthorization\s*:|\bBearer\s+[A-Za-z0-9._~+/-]{16,}/i.test(
        candidate,
      );
    });
    expect(secretVisible, "secret-shaped content reached the rendered DOM or URL").toBe(false);

    const screenshot = await page.screenshot({
      path: testInfo.outputPath(`${route.id}.png`),
      animations: "disabled",
      fullPage: true,
    });
    await testInfo.attach("screenshot", { body: screenshot, contentType: "image/png" });

    const focusOrder: FocusEvidence[] = [];
    for (let index = 0; index < 8; index += 1) {
      await page.keyboard.press(testInfo.project.name === "webkit-1440" ? "Alt+Tab" : "Tab");
      focusOrder.push(
        await page.evaluate(() => {
          const element = document.activeElement;
          return element
            ? {
                tag: element.tagName.toLowerCase(),
                role: element.getAttribute("role"),
                focus_visible: element.matches(":focus-visible"),
              }
            : null;
        }),
      );
    }
    expect(focusOrder.some((entry) => entry && entry.focus_visible)).toBeTruthy();

    const axe = await new AxeBuilder({ page }).analyze();
    const accessibility = axe.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target),
    }));
    expect(accessibility, JSON.stringify(accessibility)).toEqual([]);

    await testInfo.attach("network-summary", {
      body: Buffer.from(JSON.stringify({ requests, responses }, null, 2)),
      contentType: "application/json",
    });
    await testInfo.attach("console-summary", {
      body: Buffer.from(JSON.stringify(consoleEvents, null, 2)),
      contentType: "application/json",
    });
    await testInfo.attach("keyboard-results", {
      body: Buffer.from(JSON.stringify(focusOrder, null, 2)),
      contentType: "application/json",
    });
    await testInfo.attach("axe-results", {
      body: Buffer.from(JSON.stringify(accessibility, null, 2)),
      contentType: "application/json",
    });

    expect(failedResponses, JSON.stringify(failedResponses)).toEqual([]);
    expect(consoleErrors, JSON.stringify(consoleErrors)).toEqual([]);

    if (testInfo.project.name === "desktop-1440") {
      await page.evaluate(() => {
        document.documentElement.style.zoom = "2";
      });
      await expect(page.locator("main")).toBeVisible();
      const dimensions = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
      await testInfo.attach("zoom-200-screenshot", {
        body: await page.screenshot({ animations: "disabled", fullPage: true }),
        contentType: "image/png",
      });
      coverCriteria(testInfo, "ui.zoom-200-percent", "ui.no-document-overflow");
    }
  });
}
