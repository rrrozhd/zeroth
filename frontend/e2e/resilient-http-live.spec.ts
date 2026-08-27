import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";
import { runScenarioControl } from "./support/resilient-http-control";

const liveEnabled = process.env.ZEROTH_EVALUATION_HTTP_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = process.env.ZEROTH_EVALUATION_HTTP_WORKFLOW_ID;
const deploymentRef = process.env.ZEROTH_EVALUATION_HTTP_DEPLOYMENT_REF;
const graphVersionRef = process.env.ZEROTH_EVALUATION_HTTP_GRAPH_VERSION;
const scenarioBase = process.env.ZEROTH_EVALUATION_HTTP_SCENARIO_BASE
  ?? "http://127.0.0.1:8787";
const breakerThreshold = Number(process.env.ZEROTH_EVALUATION_HTTP_BREAKER_THRESHOLD ?? "3");
const breakerResetMs = Number(process.env.ZEROTH_EVALUATION_HTTP_BREAKER_RESET_MS ?? "1100");
const campaignId = process.env.ZEROTH_EVALUATION_CAMPAIGN_ID ?? "evaluation-studio-v1";

type Run = {
  run_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output: Record<string, unknown> | null;
  failure_state: { reason: string } | null;
};

type Evidence = {
  audits: Array<{
    audit_id: string;
    status: string;
    node_id: string;
    record_signature: string | null;
    cost_usd: number | null;
    estimated_cost_usd: number | null;
    cost_event_id: string | null;
    execution_metadata: Record<string, unknown>;
  }>;
  summary: {
    priced_call_count: number;
    total_cost_usd: number;
  };
};

const headers = () => ({ "X-API-Key": apiKey!, "X-Tenant-ID": tenant });

async function submitUi(page: Page, scenario: string): Promise<string> {
  await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, {
    waitUntil: "networkidle",
  });
  await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
  const existing = page.getByRole("dialog", { name: "Run workflow" });
  if (await existing.isVisible()) {
    await existing.getByRole("button", { name: "Close run dialog" }).click();
    await expect(existing).toBeHidden();
  }
  await page.locator('[data-evidence-id="studio.run.open"]').click();
  const dialog = page.getByRole("dialog", { name: "Run workflow" });
  // Each scenario is an independent breaker probe. Reusing the dialog's
  // persisted thread would accumulate node visits and trip the workflow loop guard.
  const newThread = dialog.getByRole("button", { name: "New", exact: true });
  if (await newThread.isVisible()) {
    await newThread.click();
  } else {
    await expect(dialog.getByRole("textbox", { name: /^Thread/ })).toHaveValue("");
  }
  await dialog.getByRole("textbox", { name: /Input payload/ }).fill(JSON.stringify({ scenario }));
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST"
  );
  await dialog.getByRole("button", { name: "Run", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  return (await response.json() as { run_id: string }).run_id;
}

async function waitTerminal(request: APIRequestContext, runId: string): Promise<Run> {
  let run: Run | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, { headers: headers() });
    expect(response.status()).toBe(200);
    run = await response.json() as Run;
    return ["succeeded", "failed", "cancelled", "terminated_by_loop_guard"].includes(run.status);
  }, { timeout: 30_000, intervals: [100, 250, 500] }).toBe(true);
  return run!;
}

async function evidence(request: APIRequestContext, runId: string): Promise<Evidence> {
  const response = await request.get(`${apiBase}/v1/runs/${runId}/evidence`, {
    headers: headers(),
  });
  expect(response.status()).toBe(200);
  return await response.json() as Evidence;
}

function httpAudit(proof: Evidence, nodeId: string) {
  const matches = proof.audits.filter((audit) => audit.node_id === nodeId);
  expect(matches, `expected one HTTP audit for ${nodeId}`).toHaveLength(1);
  const [audit] = matches;
  expect(audit.record_signature).toBeTruthy();
  expect(audit.cost_event_id).toBeNull();
  expect(audit.cost_usd).toBe(0);
  expect(audit.estimated_cost_usd).toBe(0);
  expect(audit.execution_metadata.node_kind).toBe("http_request");
  expect(audit.execution_metadata.target_url_sha256).toMatch(/^[a-f0-9]{64}$/);
  expect(JSON.stringify(audit.execution_metadata)).not.toContain("127.0.0.1");
  return audit;
}

test("resilient private GET retries, opens, recovers, and stays signed at zero provider cost", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(180_000);
  test.skip(!liveEnabled, "requires an explicitly prepared provider-free HTTP fixture");
  test.skip(
    !["desktop-1440", "webkit-1440"].includes(testInfo.project.name),
    "functional checkpoint requires Chromium and WebKit at the canonical viewport",
  );
  test.skip(
    !apiKey || !workflowId || !deploymentRef || !graphVersionRef,
    "requires exact workflow, deployment, graph, and credential identities",
  );
  coverCriteria(
    testInfo,
    "resilient-http.field-contract",
    "resilient-http.retry-success",
    "resilient-http.timeout-exhaustion",
    "resilient-http.circuit-open",
    "resilient-http.recovery",
    "resilient-http.sanitized-signed-audit",
    "resilient-http.zero-provider-economics",
  );

  await configurePage(page, apiBase, tenant, apiKey!);
  const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
  expect(runScenarioControl("POST", "/control/reset")).toEqual({ status_code: 204 });

  const health = await (await request.get(`${apiBase}/health`)).json() as {
    status: string;
    campaign_id: string;
    deployment_ref: string;
    deployment_version: number;
    graph_version_ref: string;
  };
  expect(health).toMatchObject({
    status: "ok",
    campaign_id: campaignId,
    deployment_ref: deploymentRef,
    graph_version_ref: graphVersionRef,
  });

  await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, {
    waitUntil: "networkidle",
  });
  await page.locator('.react-flow__node[data-id="http-retry"]').click();
  const inspector = page.getByRole("dialog", { name: /Edit/ });
  await expect(inspector.locator('[data-evidence-id="studio.http_request.url"]'))
    .toHaveValue(`${scenarioBase}/scenario/retry-then-success`);
  await expect(inspector.locator('[data-evidence-id="studio.http_request.max-retries"]'))
    .toHaveValue("2");
  await testInfo.attach("01-http-node-configured", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.keyboard.press("Escape");

  const retry = await waitTerminal(request, await submitUi(page, "retry"));
  expect(retry.status).toBe("succeeded");
  expect(retry.terminal_output).toMatchObject({
    http_response: { status_code: 200, body: { scenario: "retry-then-success", attempt: 3 } },
  });
  const retryEvidence = await evidence(request, retry.run_id);
  expect(httpAudit(retryEvidence, "http-retry").execution_metadata).toMatchObject({
    retry_count: 2,
    upstream_status_code: 200,
  });
  await testInfo.attach("02-retry-then-success", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const timeout = await waitTerminal(request, await submitUi(page, "timeout"));
  expect(timeout.status).toBe("failed");
  const timeoutEvidence = await evidence(request, timeout.run_id);
  expect(httpAudit(timeoutEvidence, "http-timeout").execution_metadata).toMatchObject({
    reason_code: "http_retry_exhausted_error",
    retry_count: 2,
  });
  await testInfo.attach("03-timeout-exhaustion", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const circuitRuns: Run[] = [];
  for (let index = 0; index < breakerThreshold + 1; index += 1) {
    circuitRuns.push(await waitTerminal(request, await submitUi(page, "circuit")));
  }
  expect(circuitRuns.every((run) => run.status === "failed")).toBe(true);
  const openEvidence = await evidence(request, circuitRuns.at(-1)!.run_id);
  expect(openEvidence.audits.some((audit) =>
    audit.execution_metadata.reason_code === "circuit_open_error"
  )).toBe(true);
  expect(httpAudit(openEvidence, "http-circuit").execution_metadata).toMatchObject({
    reason_code: "circuit_open_error",
    retry_count: 0,
  });
  await testInfo.attach("04-circuit-open", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  expect(runScenarioControl("POST", "/control/recover")).toEqual({ status_code: 204 });
  await page.waitForTimeout(breakerResetMs);
  const recovered = await waitTerminal(request, await submitUi(page, "circuit"));
  expect(recovered.status).toBe("succeeded");
  const recoveredEvidence = await evidence(request, recovered.run_id);
  expect(httpAudit(recoveredEvidence, "http-circuit").execution_metadata).toMatchObject({
    retry_count: 0,
    upstream_status_code: 200,
  });

  const allRuns = [retry, timeout, ...circuitRuns, recovered];
  const allEvidence = await Promise.all(allRuns.map((run) => evidence(request, run.run_id)));
  for (const proof of allEvidence) {
    expect(proof.audits.length).toBeGreaterThan(0);
    expect(proof.audits.every((audit) => Boolean(audit.record_signature))).toBe(true);
    expect(proof.audits.every((audit) => audit.cost_event_id === null)).toBe(true);
    expect(proof.summary).toMatchObject({ priced_call_count: 0, total_cost_usd: 0 });
  }
  for (const run of allRuns) {
    const verified = await request.post(`${apiBase}/v1/runs/${run.run_id}/verify-chain`, {
      headers: headers(),
    });
    expect(verified.status()).toBe(200);
    expect(await verified.json()).toMatchObject({
      verified: true,
      signature_verified: true,
      unsigned_record_count: 0,
    });
  }

  await page.goto(`/console/runs/?run=${recovered.run_id}`, { waitUntil: "networkidle" });
  await expect(page.getByText(recovered.run_id, { exact: true }).first()).toBeVisible();
  await testInfo.attach("05-recovery-run-and-signed-audit", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.goto("/console/cost/", { waitUntil: "networkidle" });
  await testInfo.attach("06-zero-provider-economics", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const scenarioEvents = runScenarioControl("GET", "/control/events");
  await attachSafeJson(testInfo, "resilient-http-summary", {
    schema_version: 1,
    health,
    runs: allRuns.map((run) => ({ run_id: run.run_id, status: run.status })),
    audits: allEvidence.flatMap((proof) => proof.audits.map((audit) => ({
      audit_id: audit.audit_id,
      node_id: audit.node_id,
      status: audit.status,
      record_signature_present: Boolean(audit.record_signature),
      cost_event_id: audit.cost_event_id,
      execution_metadata: audit.execution_metadata,
    }))),
    audit_count: allEvidence.reduce((total, proof) => total + proof.audits.length, 0),
    provider_call_count: 0,
    cost_event_ids: [],
    total_cost_usd: 0,
    scenario_events: scenarioEvents,
  });
  browserEvidence.assertNoFailedApiResponses();
  await browserEvidence.attach(testInfo);
});
