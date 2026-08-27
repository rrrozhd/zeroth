import { expect, test } from "@playwright/test";

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
const actionManifestRef = "evaluation://synthetic-action/v1";
const actionManifestDigest = "08798107120e4e1f30b56c0a77e3ea63655ec8746093f6421adf586b7aa9bf5e";

type RunResult = {
  run_id: string;
  status: string;
  terminal_output: {
    operation_key: string;
    payload_hash: string;
    receipt: string;
  } | null;
};

async function openRunDock(page: import("@playwright/test").Page) {
  const dock = page.locator(".studio-run-dock");
  const input = dock.getByRole("textbox", { name: /Input payload/ });
  if (!(await input.isVisible())) {
    await dock.getByRole("button", { name: "Run", exact: true }).click();
  }
  await expect(input).toBeVisible();
  return dock;
}

async function verifyChainInUi(page: import("@playwright/test").Page, runId: string) {
  const button = page.locator(`[data-evidence-id="runs.evidence.${runId}.verify-chain"]`);
  const response = page.waitForResponse((candidate) =>
    candidate.url().endsWith(`/v1/runs/${runId}/verify-chain`)
      && candidate.request().method() === "POST",
  );
  await button.click();
  expect((await response).status()).toBe(200);
  await expect(page.getByText(/chain intact · signatures valid/)).toBeVisible();
}

test("current governed action completes three UI approvals with signed linked receipts", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(120_000);
  coverCriteria(
    testInfo,
    "workflow3.signed-action-sink-registered",
    "workflow3.exactly-one-marker-each",
    "audit.approval-action-linkage",
    "audit.receipts-linked",
  );
  await configurePage(page, apiBase, tenant, apiKey!);
  const approvalsPage = await page.context().newPage();
  await configurePage(approvalsPage, apiBase, tenant, apiKey!);
  const browserEvidence = new BrowserEvidence(page, apiOrigin);

  const healthResponse = await request.get(`${apiBase}/health`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(healthResponse.status()).toBe(200);
  expect(await healthResponse.json()).toMatchObject({
    deployment_ref: deploymentRef,
    graph_version_ref: graphVersionRef,
    campaign_id: tenant,
  });

  const manifestPath = encodeURIComponent(actionManifestRef);
  const manifestResponse = await request.get(`${apiBase}/v1/manifests/${manifestPath}`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(manifestResponse.status()).toBe(200);
  const manifest = await manifestResponse.json() as {
    manifest_ref: string;
    side_effect: boolean;
    execution_placement: string;
    content_hash: string;
  };
  expect(manifest).toMatchObject({
    manifest_ref: actionManifestRef,
    side_effect: true,
    execution_placement: "local_only",
  });
  expect(manifest.content_hash).toMatch(/^[a-f0-9]{64}$/);

  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
  const runIds: string[] = [];
  const observations: object[] = [];

  for (let repetition = 1; repetition <= 3; repetition += 1) {
    const dock = await openRunDock(page);
    const ticket = `synthetic-ui-happy-${repetition}-${Date.now()}`;
    const input = dock.getByRole("textbox", { name: /Input payload/ });
    await input.fill(JSON.stringify({ ticket, status: "remediated" }));
    await testInfo.attach(`happy-${repetition}-configured`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const submitted = page.waitForResponse((response) =>
      response.url().endsWith("/v1/runs") && response.request().method() === "POST",
    );
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    const submission = await submitted;
    expect(submission.status()).toBe(202);
    const { run_id: runId } = await submission.json() as { run_id: string };
    runIds.push(runId);

    await expect.poll(async () => {
      const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
        headers: { "X-API-Key": apiKey! },
      });
      return (await response.json() as { status: string }).status;
    }, { timeout: 4_000, intervals: [50, 100, 200] }).toBe("paused_for_approval");
    await expect(dock.getByText("Awaiting approval", { exact: true })).toBeVisible();
    await testInfo.attach(`happy-${repetition}-pending`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await approvalsPage.goto("/console/approvals/", { waitUntil: "domcontentloaded" });
    const runLink = approvalsPage.getByRole("link", { name: runId.slice(0, 8), exact: true });
    await expect(runLink).toBeVisible({ timeout: 2_000 });
    const card = runLink.locator("xpath=ancestor::*[.//button[normalize-space()='Approve']][1]");
    await testInfo.attach(`happy-${repetition}-approval`, {
      body: await approvalsPage.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    const resolution = approvalsPage.waitForResponse((response) =>
      response.url().includes("/approvals/")
        && response.url().endsWith("/resolve")
        && response.request().method() === "POST",
    );
    await card.getByRole("button", { name: "Approve", exact: true }).click();
    expect((await resolution).status()).toBe(200);

    let terminal: RunResult | null = null;
    await expect.poll(async () => {
      const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
        headers: { "X-API-Key": apiKey! },
      });
      terminal = await response.json() as RunResult;
      return terminal.status;
    }, { timeout: 10_000, intervals: [50, 100, 250] }).toBe("succeeded");
    expect(terminal!.terminal_output).not.toBeNull();

    const verify = await (await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
      headers: { "X-API-Key": apiKey! },
    })).json() as {
      verified: boolean;
      signature_verified: boolean;
      record_count: number;
    };
    expect(verify).toMatchObject({ verified: true, signature_verified: true });
    const evidence = await (await request.get(`${apiBase}/v1/runs/${runId}/evidence`, {
      headers: { "X-API-Key": apiKey! },
    })).json() as {
      approvals: Array<{ status: string; resolution: { decision: string } | null }>;
      audits: Array<{ node_id: string; execution_metadata: Record<string, unknown> }>;
    };
    expect(evidence.approvals).toHaveLength(1);
    expect(evidence.approvals[0]).toMatchObject({
      status: "resolved",
      resolution: { decision: "approve" },
    });
    const actionAudits = evidence.audits.filter((record) =>
      record.execution_metadata?.manifest_ref_sha256 === actionManifestDigest,
    );
    expect(actionAudits).toHaveLength(1);
    expect(actionAudits[0].execution_metadata).toMatchObject({
      operation_key: terminal!.terminal_output!.operation_key,
      operation_state: "completed",
      operation_first_execution: true,
      operation_replay_suppressed: false,
      cost_usd: 0,
    });

    await page.goto(`/console/runs/?run=${runId}`, { waitUntil: "networkidle" });
    const runRow = page.locator(`[data-evidence-id="runs.run.${runId}"]`);
    await expect(runRow).toBeVisible();
    await expect(runRow).toContainText("succeeded");
    await verifyChainInUi(page, runId);
    await testInfo.attach(`happy-${repetition}-succeeded`, {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator(`[data-evidence-id="runs.run.${runId}"]`)).toBeVisible();
    await verifyChainInUi(page, runId);

    observations.push({
      repetition,
      run_id: runId,
      operation_key: terminal!.terminal_output!.operation_key,
      receipt_matches_operation: terminal!.terminal_output!.receipt.includes(
        terminal!.terminal_output!.operation_key,
      ),
      payload_hash: terminal!.terminal_output!.payload_hash,
      signed_chain_records: verify.record_count,
      action_audit_count: actionAudits.length,
    });

    await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "networkidle" });
    const currentDock = await openRunDock(page);
    const clear = currentDock.getByRole("button", { name: "Clear", exact: true });
    if (await clear.isVisible()) await clear.click();
  }

  const manifestRunsResponse = await request.get(`${apiBase}/v1/manifests/${manifestPath}/runs`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(manifestRunsResponse.status()).toBe(200);
  const manifestRuns = await manifestRunsResponse.json() as {
    runs: Array<{ run_id: string; node_id: string; status: string }>;
  };
  for (const runId of runIds) {
    expect(manifestRuns.runs).toContainEqual({
      run_id: runId,
      node_id: "synthetic-action",
      status: "completed",
    });
  }
  await attachSafeJson(testInfo, "workflow3-current-happy-runs", {
    deployment_ref: deploymentRef,
    graph_version_ref: graphVersionRef,
    manifest_content_hash: manifest.content_hash,
    runs: observations,
  });
  browserEvidence.assertNoFailedApiResponses();
  await browserEvidence.attach(testInfo);
});
