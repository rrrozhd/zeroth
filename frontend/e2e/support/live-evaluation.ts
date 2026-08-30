import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { expect, type Page, type TestInfo } from "@playwright/test";

import { sanitizeUrl, summarizeRequest, summarizeResponse } from "./sanitized-network";
import { containsSecretShape } from "./secret-shapes";

const SECRET_FIELD = /(?:^|_)(?:authorization|api_?key|provider_?key|service_?key|secret|token)(?:$|_)/i;
const IDENTITY_FIELDS = new Set([
  "campaign_id",
  "operation_id",
  "run_id",
  "thread_id",
  "audit_id",
  "cost_event_id",
  "provider_request_id",
  "authorization_log_id",
  "retry_log_id",
  "deployment_ref",
  "graph_version_ref",
]);

export type WorkflowFixture = {
  id: string;
  expectedName: string;
  expectedGraphVersion: string;
  deploymentRef: string;
  inputPayload: string;
  inspectNode: string;
  childNode?: string;
};

export function workflowFixture(index: 1 | 2 | 3): WorkflowFixture | null {
  const prefix = `ZEROTH_EVALUATION_WORKFLOW${index}`;
  const id = process.env[`${prefix}_ID`];
  const expectedGraphVersion = process.env[`${prefix}_GRAPH_VERSION`];
  const deploymentRef = process.env[`${prefix}_DEPLOYMENT_REF`];
  if (!id || !expectedGraphVersion || !deploymentRef) return null;
  return {
    id,
    expectedGraphVersion,
    deploymentRef,
    expectedName: process.env[`${prefix}_NAME`] ?? [
      "",
      "Iterative evidence research",
      "Batched investigation orchestrator",
      "Governed remediation council",
    ][index],
    inputPayload: process.env[`${prefix}_INPUT`] ?? '{"question":"evaluation fixture"}',
    inspectNode: process.env[`${prefix}_INSPECT_NODE`] ?? ["", "retrieve", "investigate-child", "approval"][index],
    childNode: index === 2
      ? process.env[`${prefix}_CHILD_NODE`] ?? "investigate-child"
      : undefined,
  };
}

function assertCredentialSafe(value: unknown, path = "$"): void {
  if (Array.isArray(value)) {
    value.forEach((child, index) => assertCredentialSafe(child, `${path}[${index}]`));
    return;
  }
  if (value && typeof value === "object") {
    for (const [key, child] of Object.entries(value)) {
      if (SECRET_FIELD.test(key) && !IDENTITY_FIELDS.has(key)) {
        throw new Error(`unsafe evidence field at ${path}.${key}`);
      }
      assertCredentialSafe(child, `${path}.${key}`);
    }
    return;
  }
  if (typeof value === "string" && containsSecretShape(value)) {
    throw new Error(`secret-shaped evidence at ${path}`);
  }
}

export async function attachSafeJson(testInfo: TestInfo, name: string, value: unknown) {
  assertCredentialSafe(value);
  await testInfo.attach(name, {
    body: Buffer.from(JSON.stringify(value, null, 2)),
    contentType: "application/json",
  });
}

export function coverCriteria(testInfo: TestInfo, ...criterionIds: string[]): void {
  for (const criterionId of criterionIds) {
    testInfo.annotations.push({ type: "criterion", description: criterionId });
  }
}

export function extractEvidenceIdentities(value: unknown): Record<string, string[]> {
  const found = new Map<string, Set<string>>();
  function visit(candidate: unknown): void {
    if (Array.isArray(candidate)) {
      candidate.forEach(visit);
      return;
    }
    if (!candidate || typeof candidate !== "object") return;
    for (const [key, child] of Object.entries(candidate)) {
      if (IDENTITY_FIELDS.has(key) && (typeof child === "string" || typeof child === "number")) {
        const rendered = String(child);
        if (!containsSecretShape(rendered)) {
          const values = found.get(key) ?? new Set<string>();
          values.add(rendered);
          found.set(key, values);
        }
      } else {
        visit(child);
      }
    }
  }
  visit(value);
  return Object.fromEntries([...found].map(([key, values]) => [key, [...values].sort()]));
}

export class BrowserEvidence {
  private readonly requests: object[] = [];
  private readonly responses: object[] = [];
  private readonly consoleEvents: object[] = [];
  private readonly failedApiResponses: { status: number; url: string }[] = [];
  private readonly identities: object[] = [];
  private readonly pendingIdentityReads: Promise<void>[] = [];

  constructor(
    private readonly page: Page,
    private readonly apiOrigin: string,
  ) {
    page.on("request", (request) => {
      this.requests.push(summarizeRequest({
        method: request.method(),
        url: request.url(),
        resourceType: request.resourceType(),
        postData: request.postData(),
      }));
    });
    page.on("response", (response) => {
      this.responses.push(summarizeResponse({
        url: response.url(),
        status: response.status(),
        resourceType: response.request().resourceType(),
      }));
      if (new URL(response.url()).origin === apiOrigin && response.status() >= 400) {
        this.failedApiResponses.push({ status: response.status(), url: sanitizeUrl(response.url()) });
      }
      const contentType = response.headers()["content-type"] ?? "";
      if (new URL(response.url()).origin === apiOrigin && contentType.includes("application/json")) {
        this.pendingIdentityReads.push(
          response.json()
            .then((body) => {
              const identity = extractEvidenceIdentities(body);
              if (Object.keys(identity).length > 0) {
                this.identities.push({ url: sanitizeUrl(response.url()), status: response.status(), identity });
              }
            })
            .catch(() => undefined),
        );
      }
    });
    page.on("console", (message) => {
      const raw = message.text();
      this.consoleEvents.push({
        type: message.type(),
        message_bytes: Buffer.byteLength(raw),
        message_sha256: createHash("sha256").update(raw).digest("hex"),
        url: message.location().url ? sanitizeUrl(message.location().url) : null,
      });
    });
  }

  assertNoFailedApiResponses(): void {
    expect(this.failedApiResponses, "evaluation UI received a 4xx/5xx API response").toEqual([]);
  }

  async attach(testInfo: TestInfo): Promise<void> {
    await Promise.all(this.pendingIdentityReads);
    await attachSafeJson(testInfo, "sanitized-network", {
      requests: this.requests,
      responses: this.responses,
    });
    await attachSafeJson(testInfo, "sanitized-console", this.consoleEvents);
    await attachSafeJson(testInfo, "response-identities", this.identities);
  }
}

export async function assertDocumentLoaded(page: Page, path: string): Promise<void> {
  const [pathname, query] = path.split("?", 2);
  const mountedPath = pathname.startsWith("/console/")
    ? pathname
    : `/console${pathname.endsWith("/") ? pathname : `${pathname}/`}`;
  const target = query == null ? mountedPath : `${mountedPath}?${query}`;
  const response = await page.goto(target, { waitUntil: "networkidle" });
  expect(response, `navigation to ${target} returned no document response`).not.toBeNull();
  expect(response!.status(), `navigation to ${target} was not successful`).toBeGreaterThanOrEqual(200);
  expect(response!.status(), `navigation to ${target} was not successful`).toBeLessThan(400);
  await expect(page.locator("main")).toBeVisible();
}

export async function assertAccessibility(page: Page, testInfo: TestInfo): Promise<void> {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  const violations = result.violations.map((violation) => ({
    id: violation.id,
    impact: violation.impact,
    targets: violation.nodes.map((node) => node.target),
  }));
  await attachSafeJson(testInfo, "axe-wcag22-aa", violations);
  expect(violations, JSON.stringify(violations)).toEqual([]);
}

export async function assertKeyboardFocus(page: Page, testInfo: TestInfo): Promise<void> {
  const order: object[] = [];
  const tabKey = process.platform === "darwin" && testInfo.project.name.startsWith("webkit")
    ? "Alt+Tab"
    : "Tab";
  for (let index = 0; index < 10; index += 1) {
    await page.keyboard.press(tabKey);
    order.push(await page.evaluate(() => {
      const active = document.activeElement;
      return {
        tag: active?.tagName.toLowerCase() ?? null,
        role: active?.getAttribute("role") ?? null,
        focus_visible: active?.matches(":focus-visible") ?? false,
      };
    }));
  }
  await attachSafeJson(testInfo, "keyboard-focus-order", { entries: order });
  expect(order.every((entry) => (entry as { focus_visible: boolean }).focus_visible)).toBe(true);
}

export async function assertMinimumTargets(page: Page, testInfo: TestInfo): Promise<void> {
  const undersized = await page.locator("button:visible, input:visible, select:visible, textarea:visible").evaluateAll(
    (elements) => elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return { tag: element.tagName.toLowerCase(), width: rect.width, height: rect.height };
    }).filter(({ width, height }) => width < 24 || height < 24),
  );
  await attachSafeJson(testInfo, "target-sizes", undersized);
  expect(undersized, "interactive controls smaller than 24 CSS pixels").toEqual([]);
}

export async function configurePage(page: Page, apiBase: string, tenant: string, apiKey: string) {
  const session = await page.request.post(`${apiBase.replace(/\/+$/, "")}/v1/auth/session`, {
    headers: { "X-API-Key": apiKey },
  });
  expect(session.status(), "browser session exchange must succeed").toBe(204);
  await page.addInitScript(({ base, evaluationTenant }) => {
    window.localStorage.setItem("zeroth.apiBase", base);
    window.localStorage.removeItem("zeroth.apiKey");
    window.localStorage.setItem("zeroth.sessionActive", "1");
    window.localStorage.setItem("zeroth.env", "local-evaluation");
    window.localStorage.setItem("zeroth.tenant", evaluationTenant);
  }, { base: apiBase, evaluationTenant: tenant });
}
