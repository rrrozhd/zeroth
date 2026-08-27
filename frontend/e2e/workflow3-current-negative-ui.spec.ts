import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

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
const workflowId = "evaluation-studio-v1-governed-remediation";
const deploymentRef = "evaluation-studio-v1-governed-remediation-v2";
const graphVersionRef = `${workflowId}@4`;
const actionManifestDigest = "08798107120e4e1f30b56c0a77e3ea63655ec8746093f6421adf586b7aa9bf5e";

type RunStatus = {
  run_id: string;
  status: string;
  terminal_output: unknown;
  failure_state: { reason?: string } | null;
};

async function openRunDock(page: Page) {
  const dock = page.locator(".studio-run-dock");
  const input = dock.getByRole("textbox", { name: /Input payload/ });
  if (!(await input.isVisible())) {
    await dock.getByRole("button", { name: "Run", exact: true }).click();
  }
  await expect(input).toBeVisible();
  return dock;
}

async function submitUiRun(page: Page, testInfo: TestInfo, scenario: string) {
  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
  const dock = await openRunDock(page);
  await dock.getByRole("textbox", { name: /Input payload/ }).fill(JSON.stringify({
    ticket: `synthetic-ui-${scenario}-${Date.now()}`,
    status: "remediated",
  }));
  await testInfo.attach(`${scenario}-configured`, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST",
  );
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  const { run_id: runId } = await response.json() as { run_id: string };
  return { dock, runId };
}

async function pollStatus(request: APIRequestContext, runId: string, expected: string) {
  let terminal: RunStatus | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    terminal = await response.json() as RunStatus;
    return terminal.status;
  }, { timeout: 25_000, intervals: [50, 100, 250, 500] }).toBe(expected);
  return terminal!;
}

async function assertRejectedEvidence(request: APIRequestContext, runId: string) {
  const verification = await (await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
    headers: { "X-API-Key": apiKey! },
  })).json() as { verified: boolean; signature_verified: boolean; record_count: number };
  expect(verification).toMatchObject({ verified: true, signature_verified: true });
  const evidence = await (await request.get(`${apiBase}/v1/runs/${runId}/evidence`, {
    headers: { "X-API-Key": apiKey! },
  })).json() as {
    approvals: Array<{
      approval_id: string;
      status: string;
      resolution: { decision: string; actor?: { subject?: string } } | null;
    }>;
    audits: Array<{ execution_metadata?: Record<string, unknown> }>;
  };
  expect(evidence.approvals).toHaveLength(1);
  expect(evidence.approvals[0]).toMatchObject({
    status: "resolved",
    resolution: { decision: "reject" },
  });
  expect(evidence.audits.filter((audit) =>
    audit.execution_metadata?.manifest_ref_sha256 === actionManifestDigest,
  )).toHaveLength(0);
  return { approval: evidence.approvals[0], verification };
}

async function verifyChainInUi(page: Page, runId: string) {
  const button = page.locator(`[data-evidence-id="runs.evidence.${runId}.verify-chain"]`);
  const response = page.waitForResponse((candidate) =>
    candidate.url().endsWith(`/v1/runs/${runId}/verify-chain`)
      && candidate.request().method() === "POST",
  );
  await button.click();
  expect((await response).status()).toBe(200);
  await expect(page.getByText(/chain intact · signatures valid/)).toBeVisible();
}

test("refresh preserves approval identity before rejection and creates no action", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(testInfo, "workflow3.negative-refresh-before-approval");
  await configurePage(page, apiBase, tenant, apiKey!);
  const browserEvidence = new BrowserEvidence(page, apiOrigin);

  const { dock, runId } = await submitUiRun(page, testInfo, "refresh-reject");
  await pollStatus(request, runId, "paused_for_approval");
  await expect(dock.getByText("Awaiting approval", { exact: true })).toBeVisible();
  await testInfo.attach("refresh-reject-pending", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const before = await (await request.get(`${apiBase}/v1/runs/${runId}/evidence`, {
    headers: { "X-API-Key": apiKey! },
  })).json() as { approvals: Array<{ approval_id: string; status: string }> };
  expect(before.approvals).toHaveLength(1);
  const approvalId = before.approvals[0].approval_id;
  expect(before.approvals[0].status).toBe("pending");

  await page.reload({ waitUntil: "networkidle" });
  const restoredDock = await openRunDock(page);
  await expect(restoredDock.getByText("Awaiting approval", { exact: true })).toBeVisible();
  await testInfo.attach("refresh-reject-restored", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.goto("/console/approvals/", { waitUntil: "networkidle" });
  const runLink = page.getByRole("link", { name: runId.slice(0, 8), exact: true });
  await expect(runLink).toBeVisible();
  const card = runLink.locator("xpath=ancestor::*[.//button[normalize-space()='Reject']][1]");
  const resolved = page.waitForResponse((response) =>
    response.url().endsWith(`/approvals/${approvalId}/resolve`)
      && response.request().method() === "POST",
  );
  await card.getByRole("button", { name: "Reject", exact: true }).click();
  expect((await resolved).status()).toBe(200);
  const terminal = await pollStatus(request, runId, "failed");
  expect(terminal).toMatchObject({ terminal_output: null, failure_state: { reason: "approval_rejected" } });
  const proof = await assertRejectedEvidence(request, runId);
  expect(proof.approval.approval_id).toBe(approvalId);

  await page.goto(`/console/runs/?run=${runId}`, { waitUntil: "networkidle" });
  const runRow = page.locator(`[data-evidence-id="runs.run.${runId}"]`);
  await expect(runRow).toContainText("failed");
  await verifyChainInUi(page, runId);
  await testInfo.attach("refresh-reject-result", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "refresh-reject-runtime", {
    run_id: runId,
    approval_id: approvalId,
    deployment_ref: deploymentRef,
    graph_version_ref: graphVersionRef,
    signed_chain_records: proof.verification.record_count,
  });
  browserEvidence.assertNoFailedApiResponses();
  await browserEvidence.attach(testInfo);
});

test("approval SLA auto-rejects and remains rejected after refresh", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(testInfo, "workflow3.negative-sla-expiry");
  await configurePage(page, apiBase, tenant, apiKey!);
  const browserEvidence = new BrowserEvidence(page, apiOrigin);

  const { dock, runId } = await submitUiRun(page, testInfo, "sla-expiry");
  await pollStatus(request, runId, "paused_for_approval");
  await expect(dock.getByText("Awaiting approval", { exact: true })).toBeVisible();
  await testInfo.attach("sla-expiry-pending", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  const terminal = await pollStatus(request, runId, "failed");
  expect(terminal).toMatchObject({ terminal_output: null, failure_state: { reason: "approval_rejected" } });
  const proof = await assertRejectedEvidence(request, runId);
  expect(proof.approval.resolution?.actor?.subject).toBe("sla_enforcer");
  await page.goto(`/console/runs/?run=${runId}`, { waitUntil: "networkidle" });
  const runRow = page.locator(`[data-evidence-id="runs.run.${runId}"]`);
  await expect(runRow).toContainText("failed");
  await verifyChainInUi(page, runId);
  await testInfo.attach("sla-expiry-result", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await page.reload({ waitUntil: "networkidle" });
  await expect(runRow).toContainText("failed");
  await verifyChainInUi(page, runId);
  await testInfo.attach("sla-expiry-restored", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "sla-expiry-runtime", {
    run_id: runId,
    approval_id: proof.approval.approval_id,
    actor: proof.approval.resolution?.actor?.subject,
    deployment_ref: deploymentRef,
    graph_version_ref: graphVersionRef,
    signed_chain_records: proof.verification.record_count,
  });
  browserEvidence.assertNoFailedApiResponses();
  await browserEvidence.attach(testInfo);
});
