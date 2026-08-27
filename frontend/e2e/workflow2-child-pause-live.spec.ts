import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

import {
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const reviewerKey = process.env.ZEROTH_EVALUATION_REVIEWER_API_KEY;
const parentWorkflow = process.env.ZEROTH_EVALUATION_W2_PAUSE_PARENT_WORKFLOW_ID;
const parentDeployment = process.env.ZEROTH_EVALUATION_W2_PAUSE_PARENT_DEPLOYMENT_REF;
const parentGraph = process.env.ZEROTH_EVALUATION_W2_PAUSE_PARENT_GRAPH_VERSION;
const childDeployment = process.env.ZEROTH_EVALUATION_W2_PAUSE_CHILD_DEPLOYMENT_REF;

const items = Array.from({ length: 8 }, (_, index) => ({
  index,
  value: `deterministic-item-${index}`,
}));
const payloadText = JSON.stringify({ items }, null, 2);
const headers = () => ({ "X-API-Key": apiKey!, "X-Tenant-ID": tenant });

type RunStatus = {
  run_id: string;
  thread_id: string;
  parent_run_id: string | null;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output: { items: Array<Record<string, unknown>> } | null;
  failure_state: { reason: string; message: string } | null;
  traversal: { node_visit_counts: Record<string, number> };
};

type ChildRun = RunStatus & { parent_run_id: string };

type Approval = {
  approval_id: string;
  run_id: string;
  deployment_ref: string;
  graph_version_ref: string;
  status: string;
  resolution: { decision: string; reason?: string | null } | null;
};

type Evidence = {
  audits: Array<{ status: string; record_signature: string | null }>;
  summary: {
    priced_call_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
    reconciliation_state: string;
  };
};

type ChildProjection = {
  run_id: string;
  thread_id: string;
  parent_run_id: string;
  branch_index: number;
  status: string;
};

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${apiBase}${path}`, { headers: headers() });
  expect(response.status(), `${path} status`).toBe(200);
  return await response.json() as T;
}

async function waitForStatus(
  request: APIRequestContext,
  runId: string,
  expected: string,
): Promise<RunStatus> {
  let observed: RunStatus | null = null;
  await expect.poll(async () => {
    observed = await getJson<RunStatus>(request, `/v1/runs/${encodeURIComponent(runId)}`);
    return observed.status;
  }, { timeout: 60_000, intervals: [100, 250, 500] }).toBe(expected);
  return observed!;
}

function branchIndex(run: RunStatus): number {
  const match = Object.keys(run.traversal.node_visit_counts)
    .map((nodeId) => /^branch:(\d+):subgraph:/.exec(nodeId))
    .find((candidate) => candidate !== null);
  expect(match, `child ${run.run_id} must expose branch-qualified traversal`).not.toBeUndefined();
  return Number(match![1]);
}

async function projectedChildren(
  request: APIRequestContext,
  parentRunId: string,
): Promise<ChildProjection[]> {
  const summaries = await getJson<ChildRun[]>(
    request,
    `/v1/runs/${encodeURIComponent(parentRunId)}/children`,
  );
  expect(summaries).toHaveLength(8);
  const runs = await Promise.all(
    summaries.map((child) =>
      getJson<ChildRun>(request, `/v1/runs/${encodeURIComponent(child.run_id)}`)
    ),
  );
  const projected = runs.map((child) => ({
    run_id: child.run_id,
    thread_id: child.thread_id,
    parent_run_id: child.parent_run_id,
    branch_index: branchIndex(child),
    status: child.status,
  })).sort((left, right) => left.branch_index - right.branch_index);
  expect(projected.map((child) => child.branch_index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  expect(new Set(projected.map((child) => child.run_id)).size).toBe(8);
  expect(new Set(projected.map((child) => child.thread_id)).size).toBe(8);
  expect(projected.every((child) => child.parent_run_id === parentRunId)).toBe(true);
  return projected;
}

async function visibleApproval(
  request: APIRequestContext,
  parentRunId: string,
): Promise<Approval> {
  const approvals = await getJson<Approval[]>(
    request,
    `/v1/deployments/${encodeURIComponent(parentDeployment!)}/approvals?run_id=${encodeURIComponent(parentRunId)}`,
  );
  expect(approvals).toHaveLength(1);
  expect(approvals[0]).toMatchObject({
    status: "pending",
    deployment_ref: childDeployment,
  });
  return approvals[0];
}

async function setBrowserKey(page: Page, key: string): Promise<void> {
  await page.evaluate((value) => window.localStorage.setItem("zeroth.apiKey", value), key);
}

async function submitFromStudio(
  page: Page,
  decision: "approve" | "reject",
  testInfo: TestInfo,
): Promise<string> {
  await page.goto(`/console/studio/edit/?id=${encodeURIComponent(parentWorkflow!)}`, {
    waitUntil: "networkidle",
  });
  await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
  const existingRunDialog = page.getByRole("dialog", { name: "Run workflow" });
  if (await existingRunDialog.isVisible()) {
    await existingRunDialog.getByRole("button", { name: "Close run dialog" }).click({
      timeout: 10_000,
    });
    await expect(existingRunDialog).toBeHidden({ timeout: 10_000 });
  }
  await page.locator('.react-flow__node[data-id="batch-input"]').click({ timeout: 10_000 });
  const batchDialog = page.getByRole("dialog", { name: "Edit Eight investigation items" });
  await batchDialog.getByRole("button", { name: "Execution" }).click({ timeout: 10_000 });
  await expect(batchDialog.getByLabel("Maximum concurrency")).toHaveValue("4");
  await expect(batchDialog.getByLabel("Batch size")).toHaveValue("8");
  await expect(batchDialog.getByLabel("Parallel failure mode")).toHaveValue("best_effort");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Run workflow" })).toBeHidden({
    timeout: 10_000,
  });
  await page.locator('[data-evidence-id="studio.run.open"]').click({ timeout: 10_000 });
  const runDialog = page.getByRole("dialog", { name: "Run workflow" });
  const input = runDialog.getByRole("textbox", { name: /Input payload/ });
  await expect(input).toBeVisible({ timeout: 10_000 });
  await input.fill(payloadText);
  await testInfo.attach(`${decision}-01-configured-studio`, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  const submitted = page.waitForResponse((response) =>
    response.url().endsWith("/v1/runs") && response.request().method() === "POST"
  );
  await runDialog.getByRole("button", { name: "Run", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  return (await response.json() as { run_id: string }).run_id;
}

async function resolveFromReviewerUi(
  page: Page,
  approval: Approval,
  decision: "approve" | "reject",
  reason: string,
  testInfo: TestInfo,
): Promise<void> {
  await setBrowserKey(page, reviewerKey!);
  await page.goto("/console/approvals/", { waitUntil: "networkidle" });
  const card = page.locator(`[data-evidence-id="approvals.card.${approval.approval_id}"]`);
  await expect(card).toBeVisible();
  await card.locator(`[data-evidence-id="approvals.reason.${approval.approval_id}"]`).fill(reason);
  await testInfo.attach(`${decision}-03-reviewer-decision`, {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  const resolved = page.waitForResponse((response) =>
    response.url().endsWith(`/approvals/${approval.approval_id}/resolve`)
      && response.request().method() === "POST"
  );
  await card.locator(`[data-evidence-id="approvals.${decision}.${approval.approval_id}"]`).click();
  const response = await resolved;
  expect(response.status()).toBe(200);
  const body = await response.json() as { approval: Approval };
  expect(body.approval).toMatchObject({
    approval_id: approval.approval_id,
    run_id: approval.run_id,
    status: "resolved",
    resolution: { decision, reason },
  });
  await setBrowserKey(page, apiKey!);
}

async function verifySignedZeroCost(
  request: APIRequestContext,
  runId: string,
): Promise<{ continuationCount: number }> {
  const verification = await request.post(`${apiBase}/v1/runs/${encodeURIComponent(runId)}/verify-chain`, {
    headers: headers(),
    data: {},
  });
  expect(verification.status()).toBe(200);
  expect(await verification.json()).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });
  const evidence = await getJson<Evidence>(request, `/v1/runs/${encodeURIComponent(runId)}/evidence`);
  expect(evidence.summary).toMatchObject({
    priced_call_count: 0,
    total_cost_usd: 0,
    cost_identity_state: "not_applicable_no_priced_call",
    reconciliation_state: "reconciled_zero_activity",
  });
  expect(evidence.audits.every((audit) => typeof audit.record_signature === "string")).toBe(true);
  return {
    continuationCount: evidence.audits.filter(
      (audit) => audit.status === "child_approval_continuation_scheduled",
    ).length,
  };
}

test.describe("Workflow 2 exact-eight child pause", () => {
  test.skip(!liveEnabled, "requires the isolated persistent evaluation service");
  test.skip(
    !apiKey || !reviewerKey || !parentWorkflow || !parentDeployment || !parentGraph
      || !childDeployment,
    "requires the exact provider-free Workflow 2 child-pause fixture",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("approves and rejects one paused child after seven siblings complete", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(180_000);
    coverCriteria(
      testInfo,
      "workflow2.negative-child-pause-partial-collection",
      "subgraphs.child-pause-and-partial-collection",
      "subgraphs.child-approval-no-sibling-replay",
      "runs.refresh-restoration",
      "audit.child-parent-signed-linkage",
      "economics.provider-free-zero-activity",
    );
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    const health = await getJson<Record<string, unknown>>(request, "/health");
    expect(health).toMatchObject({
      status: "ok",
      deployment_ref: parentDeployment,
      graph_version_ref: parentGraph,
    });

    const outcomes: Array<Record<string, unknown>> = [];
    for (const decision of ["approve", "reject"] as const) {
      const parentRunId = await submitFromStudio(page, decision, testInfo);
      const pausedParent = await waitForStatus(request, parentRunId, "paused_for_approval");
      const approval = await visibleApproval(request, parentRunId);
      const before = await projectedChildren(request, parentRunId);
      expect(before.filter((child) => child.status === "succeeded")).toHaveLength(7);
      expect(before.filter((child) => child.status === "paused_for_approval")).toEqual([
        expect.objectContaining({ branch_index: 7, run_id: approval.run_id }),
      ]);

      await page.goto(`/console/runs/?run=${encodeURIComponent(parentRunId)}`, {
        waitUntil: "networkidle",
      });
      await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText(
        "Child runs (8)",
      );
      await page.reload({ waitUntil: "networkidle" });
      await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText(
        "Child runs (8)",
      );
      const pausedLink = page.locator(`[data-evidence-id="runs.lineage.child.${approval.run_id}"]`);
      await expect(pausedLink).toBeVisible();
      await pausedLink.click();
      await expect(page.locator('[data-evidence-id="runs.lineage.parent"]')).toContainText(parentRunId);
      await testInfo.attach(`${decision}-02-paused-lineage-refresh-restored`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });

      const reason = `${decision} exact branch seven after seven completed siblings`;
      await resolveFromReviewerUi(page, approval, decision, reason, testInfo);
      const expectedParentStatus = decision === "approve" ? "succeeded" : "failed";
      const terminalParent = await waitForStatus(request, parentRunId, expectedParentStatus);
      if (decision === "approve") {
        expect(terminalParent.terminal_output).toEqual({ items });
      } else {
        expect(terminalParent.failure_state?.reason).toBe("parallel_execution_failed");
      }
      const after = await projectedChildren(request, parentRunId);
      expect(after.map((child) => child.run_id)).toEqual(before.map((child) => child.run_id));
      expect(after.slice(0, 7).every((child) => child.status === "succeeded")).toBe(true);
      expect(after[7].status).toBe(decision === "approve" ? "succeeded" : "failed");
      const parentAudit = await verifySignedZeroCost(request, parentRunId);
      expect(parentAudit.continuationCount).toBe(1);
      await Promise.all(after.map((child) => verifySignedZeroCost(request, child.run_id)));

      await page.goto(`/console/runs/?run=${encodeURIComponent(parentRunId)}`, {
        waitUntil: "networkidle",
      });
      await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText(
        "Child runs (8)",
      );
      await testInfo.attach(`${decision}-04-terminal-partial-collection`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      outcomes.push({
        decision,
        reason,
        parent_run_id: parentRunId,
        approval_id: approval.approval_id,
        approval_child_run_id: approval.run_id,
        parent_status: terminalParent.status,
        parent_failure_reason: terminalParent.failure_state?.reason ?? null,
        terminal_output: terminalParent.terminal_output,
        children_before: before,
        children_after: after,
        refresh_restored_parent_run_id: parentRunId,
        refresh_restored_approval_id: approval.approval_id,
        signed_parent_chain: true,
        signed_child_chain_count: 8,
        continuation_audit_count: parentAudit.continuationCount,
        priced_call_count: 0,
        total_cost_usd: 0,
      });
    }

    browserEvidence.assertNoFailedApiResponses();
    await browserEvidence.attach(testInfo);
    await attachSafeJson(testInfo, "workflow2-child-pause-summary", {
      schema_version: 1,
      provider_calls_performed: 0,
      provider_economics_status: "blocked",
      configured_max_concurrency: 4,
      approval_branch_index: 7,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      outcomes,
    });
  });
});
