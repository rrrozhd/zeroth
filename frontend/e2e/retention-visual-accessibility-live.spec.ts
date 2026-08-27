import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";

import { sanitizeUrl, summarizeRequest, summarizeResponse } from "./support/sanitized-network";
import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const secretRoot = process.env.ZEROTH_EVALUATION_ROLE_SECRET_ROOT
  ?? "/Users/dondoe/.local/share/zeroth/evaluations/evaluation-studio-v1/runtime-secrets";
const expectedDeploymentRef = process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF
  ?? "evaluation-studio-v1-grounded-researcher-v1";
const expectedDeploymentVersion = Number(process.env.ZEROTH_EVALUATION_DEPLOYMENT_VERSION ?? "6");
const expectedGraphVersionRef = process.env.ZEROTH_EVALUATION_GRAPH_VERSION_REF
  ?? "evaluation-studio-v1-grounded-researcher@4";
const expectedProjects = new Set([
  "desktop-1440",
  "webkit-1440",
  "desktop-1280",
  "tablet-768",
  "mobile-390",
]);

type EvidenceTarget = { id: string; locator: Locator };

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

async function assertVisibleAndNotClipped(targets: EvidenceTarget[]) {
  const geometry = [];
  for (const target of targets) {
    await expect(target.locator, `${target.id} is not visible`).toBeVisible({ timeout: 30_000 });
    geometry.push(await target.locator.evaluate((element, id) => {
      const rect = element.getBoundingClientRect();
      const root = document.documentElement;
      let ancestor: Element | null = element.parentElement;
      let hasVerticalScroller = false;
      let clippingAncestor: { tag: string; class_name: string; overflow_x: string; overflow_y: string } | null = null;
      while (ancestor && ancestor !== document.body) {
        const ancestorRect = ancestor.getBoundingClientRect();
        const style = getComputedStyle(ancestor);
        const clipsX = ["auto", "clip", "hidden", "scroll"].includes(style.overflowX);
        // A vertically scrollable app shell legitimately contains controls below
        // the current viewport; only non-scrollable clipping makes them unreachable.
        const clipsY = ["clip", "hidden"].includes(style.overflowY);
        const scrollsY = ["auto", "scroll"].includes(style.overflowY)
          && ancestor.scrollHeight > ancestor.clientHeight;
        if (
          (clipsX && (rect.left < ancestorRect.left - 1 || rect.right > ancestorRect.right + 1))
          || (
            clipsY
            && !hasVerticalScroller
            && (rect.top < ancestorRect.top - 1 || rect.bottom > ancestorRect.bottom + 1)
          )
        ) {
          clippingAncestor = {
            tag: ancestor.tagName.toLowerCase(),
            class_name: ancestor.className,
            overflow_x: style.overflowX,
            overflow_y: style.overflowY,
          };
          break;
        }
        hasVerticalScroller ||= scrollsY;
        ancestor = ancestor.parentElement;
      }
      return {
        id,
        x: rect.x,
        y: rect.y + window.scrollY,
        width: rect.width,
        height: rect.height,
        right: rect.right,
        document_width: root.clientWidth,
        clipped_by_ancestor: clippingAncestor,
        horizontally_in_document: rect.left >= -1 && rect.right <= root.clientWidth + 1,
        has_area: rect.width > 0 && rect.height > 0,
      };
    }, target.id));
  }
  expect(geometry.filter((item) => (
    !item.has_area || !item.horizontally_in_document || item.clipped_by_ancestor !== null
  )), JSON.stringify(geometry, null, 2)).toEqual([]);
  return geometry;
}

async function assertEnabledTargetSizes(page: Page) {
  const sizes = await page.locator("main").evaluate((main) => {
    const enabled = Array.from(main.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    return enabled.flatMap((element) => {
      const ownRect = element.getBoundingClientRect();
      if (ownRect.width === 0 || ownRect.height === 0 || getComputedStyle(element).visibility === "hidden") {
        return [];
      }
      const target = element.matches('input[type="checkbox"], input[type="radio"]')
        ? element.closest("label") ?? element
        : element;
      const rect = target.getBoundingClientRect();
      return [{
        tag: element.tagName.toLowerCase(),
        name: element.getAttribute("aria-label")
          ?? element.getAttribute("data-evidence-id")
          ?? element.textContent?.trim().slice(0, 80)
          ?? null,
        width: rect.width,
        height: rect.height,
        meets_minimum: rect.width >= 24 && rect.height >= 24,
      }];
    });
  });
  expect(sizes.filter((item) => !item.meets_minimum), JSON.stringify(sizes, null, 2)).toEqual([]);
  return sizes;
}

test.describe("Retention visual accessibility live matrix", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test("authorized Retention UI reflows, focuses, and remains accessible", async ({ page, request }, testInfo) => {
    test.setTimeout(60_000);
    expect(expectedProjects.has(testInfo.project.name), `unexpected project ${testInfo.project.name}`).toBe(true);
    coverCriteria(testInfo, "product.retention.responsive-and-zoom", "ui.no-document-overflow");
    if (testInfo.project.name === "webkit-1440") {
      coverCriteria(testInfo, "product.retention.webkit-axe-and-keyboard");
    }

    const apiKey = readFileSync(path.join(secretRoot, "service-api-key"), "utf8").trim();
    expect(apiKey, "external platform-admin credential is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey);
    await page.addInitScript(() => {
      const probe = { count: 0 };
      Object.defineProperty(window, "__zerothUnhandledRejections", { value: probe });
      window.addEventListener("unhandledrejection", () => { probe.count += 1; });
    });
    await page.emulateMedia({ reducedMotion: "reduce" });

    const requests: object[] = [];
    const responses: object[] = [];
    const failedResponses: object[] = [];
    const consoleEvents: object[] = [];
    const consoleErrors: object[] = [];
    const pageErrors: object[] = [];
    page.on("request", (entry) => {
      requests.push(summarizeRequest({
        method: entry.method(),
        url: entry.url(),
        resourceType: entry.resourceType(),
        postData: entry.postData(),
      }));
    });
    page.on("response", (entry) => {
      const summary = summarizeResponse({
        url: entry.url(),
        status: entry.status(),
        resourceType: entry.request().resourceType(),
      });
      responses.push(summary);
      if (entry.status() >= 400) failedResponses.push(summary);
    });
    page.on("console", (message) => {
      const value = message.text();
      const summary = {
        type: message.type(),
        message_bytes: Buffer.byteLength(value),
        message_sha256: digest(value),
        url: message.location().url ? sanitizeUrl(message.location().url) : null,
      };
      consoleEvents.push(summary);
      if (message.type() === "error") consoleErrors.push(summary);
    });
    page.on("pageerror", (error) => {
      pageErrors.push({ message_bytes: Buffer.byteLength(error.message), message_sha256: digest(error.message) });
    });

    const headers = { "X-API-Key": apiKey };
    const [identityResponse, healthResponse] = await Promise.all([
      request.get(`${apiBase}/v1/identity`, { headers }),
      request.get(`${apiBase}/health`),
    ]);
    expect(identityResponse.status()).toBe(200);
    expect(healthResponse.status()).toBe(200);
    const identity = await identityResponse.json() as {
      tenant_id: string;
      workspace_id: string | null;
      roles: string[];
    };
    const health = await healthResponse.json() as {
      deployment_ref: string;
      deployment_version: number;
      graph_version_ref: string;
    };
    expect(identity.tenant_id).toBe(tenant);
    expect(identity.workspace_id).toBeNull();
    expect(identity.roles).toEqual(["platform_admin"]);
    expect(health.deployment_ref).toBe(expectedDeploymentRef);
    expect(health.deployment_version).toBe(expectedDeploymentVersion);
    expect(health.graph_version_ref).toBe(expectedGraphVersionRef);

    const documentResponse = await page.goto("/console/retention/", { waitUntil: "domcontentloaded" });
    expect(documentResponse?.status()).toBeGreaterThanOrEqual(200);
    expect(documentResponse?.status()).toBeLessThan(400);
    await expect(page.getByRole("heading", { name: "Retention & Compliance", exact: true })).toBeVisible();
    await expect(page.locator(".console-topbar-breadcrumb")).toHaveAttribute(
      "aria-label",
      `Scope: ${tenant} / tenant-wide; roles: platform_admin`,
    );
    await expect(page.locator(".console-topbar-role")).toHaveText("platform_admin");

    const policyCard = page.locator('[data-evidence-id="retention.policy.card"]');
    const holdsCard = page.locator('[data-evidence-id="retention.legal-holds.card"]');
    const erasureCard = page.locator('[data-evidence-id="retention.erasure.card"]');
    await expect(erasureCard, "Erasure heading must resolve one owning card").toHaveCount(1);
    const enabled = page.getByRole("checkbox", { name: "Retention enforcement enabled" });
    const runTtl = page.getByRole("textbox", { name: "Run payloads TTL in days" });
    const auditTtl = page.getByRole("textbox", { name: "Audit records TTL in days" });
    const legalRun = page.getByRole("textbox", { name: "Legal hold run ID" });
    const legalReason = page.getByRole("textbox", { name: "Legal hold reason" });
    const placeHold = page.getByRole("button", { name: "Place hold", exact: true });
    const singleRun = page.locator('[data-evidence-id="retention.erasure.scope.run"]');
    const entireTenant = page.locator('[data-evidence-id="retention.erasure.scope.tenant"]');
    const erasureRun = page.locator('[data-evidence-id="retention.erasure.run-id"]');
    const erasureNote = page.locator('[data-evidence-id="retention.erasure.note"]');
    const stageErasure = page.locator('[data-evidence-id="retention.erasure.stage"]');
    const savePolicy = page.getByRole("button", { name: "Save policy", exact: true });
    await expect(legalRun, "legal-hold data must settle before tab order is inventoried").toBeVisible({
      timeout: 30_000,
    });
    const releaseButtons = await holdsCard.getByRole("button", { name: /Release legal hold/ }).all();
    const targets: EvidenceTarget[] = [
      { id: "policy-card", locator: policyCard },
      { id: "legal-holds-card", locator: holdsCard },
      { id: "erasure-card", locator: erasureCard },
      { id: "retention-enabled", locator: enabled },
      { id: "run-ttl", locator: runTtl },
      { id: "audit-ttl", locator: auditTtl },
      { id: "save-policy", locator: savePolicy },
      ...releaseButtons.map((locator, index) => ({ id: `release-hold-${index}`, locator })),
      { id: "legal-run", locator: legalRun },
      { id: "legal-reason", locator: legalReason },
      { id: "place-hold", locator: placeHold },
      { id: "single-run", locator: singleRun },
      { id: "entire-tenant", locator: entireTenant },
      { id: "erasure-run", locator: erasureRun },
      { id: "erasure-note", locator: erasureNote },
      { id: "stage-erasure", locator: stageErasure },
    ];
    const geometry = await assertVisibleAndNotClipped(targets);
    const dimensions = await page.evaluate(() => ({
      client_width: document.documentElement.clientWidth,
      scroll_width: document.documentElement.scrollWidth,
      reduced_motion: matchMedia("(prefers-reduced-motion: reduce)").matches,
    }));
    expect(dimensions.scroll_width).toBeLessThanOrEqual(dimensions.client_width + 1);
    expect(dimensions.reduced_motion).toBe(true);
    const targetSizes = await assertEnabledTargetSizes(page);

    await testInfo.attach("retention-visual-accessibility", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const focusSequence = [
      runTtl,
      auditTtl,
      ...releaseButtons,
      legalRun,
      legalReason,
      placeHold,
      singleRun,
      entireTenant,
      erasureRun,
      erasureNote,
    ];
    const focusEvidence: object[] = [];
    const originallyChecked = await enabled.isChecked();
    await enabled.focus();
    await page.keyboard.press("Space");
    expect(await enabled.isChecked()).toBe(!originallyChecked);
    await page.keyboard.press("Space");
    expect(await enabled.isChecked()).toBe(originallyChecked);
    const tabKey = process.platform === "darwin" && testInfo.project.name.startsWith("webkit")
      ? "Alt+Tab"
      : "Tab";
    for (const control of focusSequence) {
      await page.keyboard.press(tabKey);
      await expect(control).toBeFocused();
      const focusVisible = await control.evaluate((element) => element.matches(":focus-visible"));
      expect(focusVisible).toBe(true);
      focusEvidence.push({
        tag: await control.evaluate((element) => element.tagName.toLowerCase()),
        aria_label: await control.getAttribute("aria-label"),
        evidence_id: await control.getAttribute("data-evidence-id"),
        focus_visible: focusVisible,
      });
    }

    const axe = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    const violations = axe.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target),
    }));
    expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);

    const unhandledRejections = await page.evaluate(() => (
      window as typeof window & { __zerothUnhandledRejections: { count: number } }
    ).__zerothUnhandledRejections.count);
    expect(unhandledRejections).toBe(0);
    expect(consoleErrors, JSON.stringify(consoleErrors, null, 2)).toEqual([]);
    expect(pageErrors, JSON.stringify(pageErrors, null, 2)).toEqual([]);
    expect(failedResponses, JSON.stringify(failedResponses, null, 2)).toEqual([]);
    expect(
      requests.filter((entry) => {
        const method = (entry as { method?: string }).method;
        const url = (entry as { url?: string }).url ?? "";
        return url.includes("/v1/retention/") && method !== "GET";
      }),
      "visual acceptance must not mutate Retention state",
    ).toEqual([]);

    await attachSafeJson(testInfo, "retention-viewport-role-tenant", {
      project: testInfo.project.name,
      viewport: page.viewportSize(),
      tenant_id: identity.tenant_id,
      workspace_id: identity.workspace_id,
      role: "platform_admin",
      deployment_ref: health.deployment_ref,
      deployment_version: health.deployment_version,
      graph_version_ref: health.graph_version_ref,
      geometry,
      target_sizes: targetSizes,
      document: dimensions,
    });
    await attachSafeJson(testInfo, "retention-keyboard-focus", focusEvidence);
    await attachSafeJson(testInfo, "retention-axe-wcag22-aa", violations);
    await attachSafeJson(testInfo, "retention-sanitized-network", { requests, responses, failed_responses: failedResponses });
    await attachSafeJson(testInfo, "retention-sanitized-console", {
      events: consoleEvents,
      errors: consoleErrors,
      page_errors: pageErrors,
      unhandled_rejections: unhandledRejections,
    });

    if (testInfo.project.name === "desktop-1440") {
      coverCriteria(testInfo, "ui.zoom-200-percent");
      await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
      const zoomGeometry = await assertVisibleAndNotClipped(targets.slice(0, 3));
      const zoomDimensions = await page.evaluate(() => ({
        client_width: document.documentElement.clientWidth,
        scroll_width: document.documentElement.scrollWidth,
      }));
      expect(zoomDimensions.scroll_width).toBeLessThanOrEqual(zoomDimensions.client_width + 1);
      await assertEnabledTargetSizes(page);
      await testInfo.attach("retention-zoom-200", {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      await attachSafeJson(testInfo, "retention-zoom-200-geometry", {
        project: testInfo.project.name,
        zoom_percent: 200,
        geometry: zoomGeometry,
        document: zoomDimensions,
      });
    }
  });
});
