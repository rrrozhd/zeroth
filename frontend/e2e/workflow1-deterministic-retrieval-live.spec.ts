import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

import {
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const enabled = process.env.ZEROTH_EVALUATION_W1_DETERMINISTIC_RETRIEVAL === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const credential = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = process.env.ZEROTH_EVALUATION_W1_RETRIEVAL_WORKFLOW_ID;
const deploymentRef = process.env.ZEROTH_EVALUATION_W1_RETRIEVAL_DEPLOYMENT_REF;
const graphRef = process.env.ZEROTH_EVALUATION_W1_RETRIEVAL_GRAPH_VERSION;
const emptyConnector = process.env.ZEROTH_EVALUATION_W1_EMPTY_CONNECTOR;
const conflictConnector = process.env.ZEROTH_EVALUATION_W1_CONFLICT_CONNECTOR;
const headers = () => ({
  [["X", "API", "Key"].join("-")]: credential!,
  [["X", "Tenant", "ID"].join("-")]: tenant,
});

type Audit = {
  audit_id: string;
  node_id: string;
  status: string;
  record_signature: string | null;
  cost_event_id: string | null;
  cost_usd: number | null;
  execution_metadata: {
    provider_request_id?: string | null;
    retrieval_result_count?: number;
  };
};
type Run = {
  run_id: string;
  thread_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output: { scenario: string; query: string; answer: string; source_ids: string[] } | null;
  traversal: { node_visit_counts: Record<string, number> };
};
type Evidence = {
  audits: Audit[];
  summary: {
    priced_call_count: number;
    cost_event_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
    reconciliation_state: string;
  };
};

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${apiBase}${path}`, { headers: headers() });
  expect(response.status(), path).toBe(200);
  return await response.json() as T;
}

async function runScenario(
  page: Page,
  request: APIRequestContext,
  scenario: "no_result" | "conflict",
): Promise<{ run: Run; evidence: Evidence; chain: Record<string, unknown> }> {
  const payload = scenario === "no_result"
    ? { scenario, query: "tenant corpus answer that does not exist" }
    : { scenario, query: "approved queue depth" };
  await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, {
    waitUntil: "networkidle",
  });
  const existing = page.getByRole("dialog", { name: "Run workflow" });
  if (await existing.isVisible()) {
    await existing.getByRole("button", { name: "Close run dialog" }).click();
    await expect(existing).toBeHidden();
  }
  await page.locator('[data-evidence-id="studio.run.open"]').click();
  const dialog = page.getByRole("dialog", { name: "Run workflow" });
  await dialog.getByRole("textbox", { name: /Input payload/ }).fill(JSON.stringify(payload, null, 2));
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST"
  );
  await dialog.getByRole("button", { name: "Run", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  const runId = (await response.json() as { run_id: string }).run_id;
  let run: Run | null = null;
  await expect.poll(async () => {
    run = await getJson<Run>(request, `/v1/runs/${encodeURIComponent(runId)}`);
    return run.status;
  }, { timeout: 45_000, intervals: [100, 250, 500] }).toBe("succeeded");
  const chainResponse = await request.post(
    `${apiBase}/v1/runs/${encodeURIComponent(runId)}/verify-chain`,
    { headers: headers(), data: {} },
  );
  expect(chainResponse.status()).toBe(200);
  const chain = await chainResponse.json() as Record<string, unknown>;
  expect(chain).toMatchObject({ verified: true, signature_verified: true, unsigned_record_count: 0 });
  const evidence = await getJson<Evidence>(request, `/v1/runs/${encodeURIComponent(runId)}/evidence`);
  expect(evidence.audits.every((audit) => typeof audit.record_signature === "string")).toBe(true);
  expect(evidence.audits.every((audit) => audit.cost_event_id === null)).toBe(true);
  expect(evidence.audits.every((audit) => audit.execution_metadata.provider_request_id == null)).toBe(true);
  expect(evidence.summary).toMatchObject({
    priced_call_count: 0,
    cost_event_count: 0,
    total_cost_usd: 0,
    cost_identity_state: "not_applicable_no_priced_call",
    reconciliation_state: "reconciled_zero_activity",
  });
  const retrieval = evidence.audits.find((audit) => audit.node_id.endsWith("retrieval"));
  expect(retrieval).toBeDefined();
  expect(retrieval!.execution_metadata.retrieval_result_count).toBe(
    scenario === "no_result" ? 0 : 2,
  );
  return { run: run!, evidence, chain };
}

async function screenshotRun(page: Page, run: Run, label: string, testInfo: TestInfo) {
  await page.goto(`/console/runs/?run=${encodeURIComponent(run.run_id)}`, { waitUntil: "networkidle" });
  await expect(page.getByText(run.terminal_output!.answer, { exact: false })).toBeVisible();
  await testInfo.attach(label, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
}

test.describe("Workflow 1 deterministic local Chroma negatives", () => {
  test.skip(!enabled, "requires the isolated local Chroma fixture");
  test.skip(!credential || !workflowId || !deploymentRef || !graphRef || !emptyConnector || !conflictConnector,
    "requires exact fixture identities");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, credential!);
  });

  test("abstains on no result and reports both conflicting documents", async ({ page, request }, testInfo) => {
    test.setTimeout(120_000);
    coverCriteria(
      testInfo,
      "workflow1.negative-no-result",
      "workflow1.negative-conflicting-document",
    );
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    const health = await getJson<Record<string, unknown>>(request, "/health");
    expect(health).toMatchObject({
      status: "ok",
      deployment_ref: deploymentRef,
      graph_version_ref: graphRef,
    });

    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, { waitUntil: "networkidle" });
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
    await page.locator('.react-flow__node[data-id="empty-retrieval"]').click();
    const empty = page.getByRole("dialog", { name: "Edit Search empty tenant corpus" });
    await expect(empty.getByRole("combobox", { name: /^Connector / })).toHaveValue(emptyConnector!);
    await testInfo.attach("01-empty-tenant-corpus-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.keyboard.press("Escape");
    await page.locator('.react-flow__node[data-id="conflict-retrieval"]').click();
    const conflict = page.getByRole("dialog", { name: "Edit Search conflicting tenant corpus" });
    await expect(conflict.getByRole("combobox", { name: /^Connector / })).toHaveValue(conflictConnector!);
    await expect(conflict.getByLabel("Top K")).toHaveValue("2");
    await testInfo.attach("02-conflicting-tenant-corpus-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.keyboard.press("Escape");

    const noResult = await runScenario(page, request, "no_result");
    expect(noResult.run.terminal_output).toEqual({
      scenario: "no_result",
      query: "tenant corpus answer that does not exist",
      answer: "No grounded result found in the tenant-scoped corpus.",
      source_ids: [],
    });
    await screenshotRun(page, noResult.run, "03-no-result-abstention", testInfo);
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText("No grounded result found in the tenant-scoped corpus.", { exact: false })).toBeVisible();
    await testInfo.attach("04-no-result-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const conflictResult = await runScenario(page, request, "conflict");
    expect(conflictResult.run.terminal_output).toEqual({
      scenario: "conflict",
      query: "approved queue depth",
      answer: "Conflict detected: approved and obsolete documents disagree.",
      source_ids: ["approved-queue-depth-four", "obsolete-queue-depth-six"],
    });
    await screenshotRun(page, conflictResult.run, "05-conflict-both-sources", testInfo);
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText("Conflict detected: approved and obsolete documents disagree.", { exact: false })).toBeVisible();
    await testInfo.attach("06-conflict-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const allAudits = [...noResult.evidence.audits, ...conflictResult.evidence.audits];
    browserEvidence.assertNoFailedApiResponses();
    await browserEvidence.attach(testInfo);
    await attachSafeJson(testInfo, "workflow1-deterministic-retrieval-summary", {
      schema_version: 1,
      health,
      provider_calls_performed: 0,
      provider_request_ids: [],
      cost_event_ids: [],
      total_cost_usd: 0,
      runs: [
        {
          ...noResult.run,
          retrieval_result_count: 0,
          audit_ids: noResult.evidence.audits.map((audit) => audit.audit_id),
          chain: noResult.chain,
        },
        {
          ...conflictResult.run,
          retrieval_result_count: 2,
          audit_ids: conflictResult.evidence.audits.map((audit) => audit.audit_id),
          chain: conflictResult.chain,
        },
      ],
      signed_audit_count: allAudits.length,
      refresh_restored_run_ids: [noResult.run.run_id, conflictResult.run.run_id],
    });
  });
});
