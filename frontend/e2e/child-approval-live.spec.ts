import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

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
const parentDeployment = process.env.ZEROTH_EVALUATION_D012_PARENT_DEPLOYMENT_REF;
const parentGraph = process.env.ZEROTH_EVALUATION_D012_PARENT_GRAPH_VERSION;
const approvalDeployment = process.env.ZEROTH_EVALUATION_D012_APPROVAL_DEPLOYMENT_REF;
const durableDeployment = process.env.ZEROTH_EVALUATION_D012_DURABLE_DEPLOYMENT_REF;
const preRestartParent = process.env.ZEROTH_EVALUATION_D012_PRE_RESTART_PARENT_RUN_ID;
const preRestartApproval = process.env.ZEROTH_EVALUATION_D012_PRE_RESTART_APPROVAL_ID;
const preRestartChild = process.env.ZEROTH_EVALUATION_D012_PRE_RESTART_CHILD_RUN_ID;

const headers = () => ({ "X-API-Key": apiKey!, "X-Tenant-ID": tenant });

type RunStatus = {
  run_id: string;
  thread_id: string;
  parent_run_id: string | null;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  terminal_output: Record<string, unknown> | null;
  failure_state: { reason: string; message: string } | null;
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
  audits: Array<{
    audit_id: string;
    run_id: string;
    status: string;
    record_signature: string | null;
  }>;
  approvals: Approval[];
  summary: {
    priced_call_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
    reconciliation_state: string;
  };
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
  }, { timeout: 45_000, intervals: [100, 250, 500] }).toBe(expected);
  return observed!;
}

async function childRuns(request: APIRequestContext, runId: string): Promise<ChildRun[]> {
  return getJson(request, `/v1/runs/${encodeURIComponent(runId)}/children`);
}

function durableSiblingCount(children: ChildRun[]): number {
  return children.filter((child) => child.deployment_ref === durableDeployment).length;
}

async function verifySignedZeroCost(
  request: APIRequestContext,
  runId: string,
): Promise<{ evidence: Evidence; continuationCount: number }> {
  const chainResponse = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
    headers: headers(),
    data: {},
  });
  expect(chainResponse.status()).toBe(200);
  expect(await chainResponse.json()).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });
  const evidence = await getJson<Evidence>(request, `/v1/runs/${runId}/evidence`);
  expect(evidence.summary).toMatchObject({
    priced_call_count: 0,
    total_cost_usd: 0,
    cost_identity_state: "not_applicable_no_priced_call",
    reconciliation_state: "reconciled_zero_activity",
  });
  expect(evidence.audits.every((audit) => typeof audit.record_signature === "string")).toBe(true);
  const continuationCount = evidence.audits.filter(
    (audit) => audit.status === "child_approval_continuation_scheduled",
  ).length;
  expect(continuationCount).toBe(1);
  return { evidence, continuationCount };
}

async function resolveFromUi(
  page: Page,
  approval: Approval,
  decision: "approve" | "reject",
  reason: string,
): Promise<Approval> {
  await page.goto("/console/approvals/", { waitUntil: "networkidle" });
  const card = page.locator(`[data-evidence-id="approvals.card.${approval.approval_id}"]`);
  await expect(card).toBeVisible();
  await expect(card).toContainText(approval.run_id.slice(0, 8));
  await card.locator(`[data-evidence-id="approvals.reason.${approval.approval_id}"]`).fill(reason);
  const responsePromise = page.waitForResponse((response) =>
    response.url().endsWith(`/approvals/${approval.approval_id}/resolve`)
      && response.request().method() === "POST"
  );
  await card.locator(
    `[data-evidence-id="approvals.${decision}.${approval.approval_id}"]`,
  ).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const body = await response.json() as { approval: Approval };
  expect(body.approval).toMatchObject({
    approval_id: approval.approval_id,
    run_id: approval.run_id,
    deployment_ref: approvalDeployment,
    status: "resolved",
    resolution: { decision, reason },
  });
  return body.approval;
}

async function submitParent(request: APIRequestContext): Promise<RunStatus> {
  const response = await request.post(`${apiBase}/v1/runs`, {
    headers: headers(),
    data: {
      input_payload: { request: "d012-provider-free" },
      campaign_id: "evaluation-studio-v1",
      campaign_strict: true,
    },
  });
  expect(response.status()).toBe(202);
  const created = await response.json() as { run_id: string };
  return waitForStatus(request, created.run_id, "paused_for_approval");
}

async function soleVisibleApproval(request: APIRequestContext, parentRunId: string): Promise<Approval> {
  const approvals = await getJson<Approval[]>(
    request,
    `/v1/deployments/${encodeURIComponent(parentDeployment!)}/approvals?run_id=${encodeURIComponent(parentRunId)}`,
  );
  expect(approvals).toHaveLength(1);
  expect(approvals[0]).toMatchObject({
    deployment_ref: approvalDeployment,
    status: "pending",
  });
  expect(approvals[0].run_id).not.toBe(parentRunId);
  return approvals[0];
}

test.describe("provider-free child approval persistence", () => {
  test.skip(!liveEnabled, "requires the isolated persistent evaluation service");
  test.skip(
    !apiKey || !parentDeployment || !parentGraph || !approvalDeployment || !durableDeployment
      || !preRestartParent || !preRestartApproval || !preRestartChild,
    "requires a coordinated pre-restart D-012 staging identity",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("resolves exact child approve and reject after one coordinated restart", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(150_000);
    coverCriteria(
      testInfo,
      "subgraphs.child-approval-parent-visibility",
      "subgraphs.child-approval-restart-restoration",
      "subgraphs.child-approval-no-sibling-replay",
      "approvals.reason-ui",
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

    const stagedParent = await waitForStatus(request, preRestartParent!, "paused_for_approval");
    expect(stagedParent).toMatchObject({
      run_id: preRestartParent,
      deployment_ref: parentDeployment,
      graph_version_ref: parentGraph,
      parent_run_id: null,
    });
    const stagedApproval = await soleVisibleApproval(request, stagedParent.run_id);
    expect(stagedApproval).toMatchObject({
      approval_id: preRestartApproval,
      run_id: preRestartChild,
    });
    const approveChildrenBefore = await childRuns(request, stagedParent.run_id);
    expect(durableSiblingCount(approveChildrenBefore)).toBe(1);

    await page.goto("/console/approvals/", { waitUntil: "networkidle" });
    const stagedCardSelector = `[data-evidence-id="approvals.card.${stagedApproval.approval_id}"]`;
    await expect(page.locator(stagedCardSelector)).toBeVisible();
    await testInfo.attach("01-parent-scoped-child-approval-after-restart", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator(stagedCardSelector)).toBeVisible();
    const approveReason = "Verified durable sibling delivery and exact child identity";
    await resolveFromUi(page, stagedApproval, "approve", approveReason);
    const approvedParent = await waitForStatus(request, stagedParent.run_id, "succeeded");
    expect(approvedParent.terminal_output).toEqual({
      branches: [{ branch: "durable" }, { branch: "approval" }],
    });
    const approveChildrenAfter = await childRuns(request, stagedParent.run_id);
    expect(durableSiblingCount(approveChildrenAfter)).toBe(1);
    const approvedAudit = await verifySignedZeroCost(request, stagedParent.run_id);
    await page.goto(`/console/runs/?run=${encodeURIComponent(stagedParent.run_id)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toBeVisible();
    await testInfo.attach("02-approved-parent-exact-child-complete", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const rejectParent = await submitParent(request);
    const rejectApproval = await soleVisibleApproval(request, rejectParent.run_id);
    const rejectChildrenBefore = await childRuns(request, rejectParent.run_id);
    expect(durableSiblingCount(rejectChildrenBefore)).toBe(1);
    const rejectReason = "Rejected the controlled provider-free approval branch";
    await page.goto("/console/approvals/", { waitUntil: "networkidle" });
    await testInfo.attach("03-reject-reason-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await resolveFromUi(page, rejectApproval, "reject", rejectReason);
    const rejectedParent = await waitForStatus(request, rejectParent.run_id, "failed");
    expect(rejectedParent.failure_state?.reason).toBe("parallel_execution_failed");
    const rejectChildrenAfter = await childRuns(request, rejectParent.run_id);
    expect(durableSiblingCount(rejectChildrenAfter)).toBe(1);
    const rejectedAudit = await verifySignedZeroCost(request, rejectParent.run_id);
    await page.goto(`/console/runs/?run=${encodeURIComponent(rejectParent.run_id)}`, {
      waitUntil: "networkidle",
    });
    await testInfo.attach("04-rejected-parent-partial-collection", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    browserEvidence.assertNoFailedApiResponses();
    await browserEvidence.attach(testInfo);
    await attachSafeJson(testInfo, "child-approval-live-summary", {
      schema_version: 1,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      provider_calls_performed: 0,
      provider_economics_status: "blocked",
      restart_count: 1,
      approvals: [
        {
          decision: "approve",
          reason: approveReason,
          approval_id: stagedApproval.approval_id,
          child_run_id: stagedApproval.run_id,
          parent_run_id: stagedParent.run_id,
          parent_status: approvedParent.status,
          durable_sibling_delivery_count_before: durableSiblingCount(approveChildrenBefore),
          durable_sibling_delivery_count_after: durableSiblingCount(approveChildrenAfter),
          continuation_audit_count: approvedAudit.continuationCount,
          signed_audit: true,
          priced_call_count: approvedAudit.evidence.summary.priced_call_count,
          total_cost_usd: approvedAudit.evidence.summary.total_cost_usd,
          restored_after_refresh: true,
          restored_after_restart: true,
        },
        {
          decision: "reject",
          reason: rejectReason,
          approval_id: rejectApproval.approval_id,
          child_run_id: rejectApproval.run_id,
          parent_run_id: rejectParent.run_id,
          parent_status: rejectedParent.status,
          durable_sibling_delivery_count_before: durableSiblingCount(rejectChildrenBefore),
          durable_sibling_delivery_count_after: durableSiblingCount(rejectChildrenAfter),
          continuation_audit_count: rejectedAudit.continuationCount,
          signed_audit: true,
          priced_call_count: rejectedAudit.evidence.summary.priced_call_count,
          total_cost_usd: rejectedAudit.evidence.summary.total_cost_usd,
          restored_after_refresh: true,
          restored_after_restart: true,
        },
      ],
    });
  });
});
