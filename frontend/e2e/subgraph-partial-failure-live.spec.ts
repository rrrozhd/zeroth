import { expect, test, type APIRequestContext } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const parentWorkflowId = process.env.ZEROTH_EVALUATION_PARTIAL_PARENT_WORKFLOW_ID;
const parentDeploymentRef = process.env.ZEROTH_EVALUATION_PARTIAL_PARENT_DEPLOYMENT_REF;
const parentGraphVersion = process.env.ZEROTH_EVALUATION_PARTIAL_PARENT_GRAPH_VERSION;
const childDeploymentRef = process.env.ZEROTH_EVALUATION_PARTIAL_CHILD_DEPLOYMENT_REF;
const payloadText = process.env.ZEROTH_EVALUATION_PARTIAL_PAYLOAD;

type RunStatus = {
  run_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  thread_id: string;
  parent_run_id: string | null;
  terminal_output: { items: Array<Record<string, unknown> | null> } | null;
  failure_state: { reason: string; message: string } | null;
  traversal: { node_visit_counts: Record<string, number> };
};

type ChildSummary = Pick<
  RunStatus,
  "run_id" | "status" | "deployment_ref" | "graph_version_ref" | "thread_id"
> & { parent_run_id: string };

type RunEvidence = {
  audits: Array<{ audit_id: string; node_id: string; record_signature: string | null }>;
  summary: {
    priced_call_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
    reconciliation_state: string;
  };
};

function branchIndex(run: RunStatus): number {
  const match = Object.keys(run.traversal.node_visit_counts)
    .map((nodeId) => /^branch:(\d+):subgraph:/.exec(nodeId))
    .find((candidate) => candidate !== null);
  expect(match, `child ${run.run_id} must expose branch-qualified traversal`).not.toBeUndefined();
  return Number(match![1]);
}

async function getJson<T>(
  request: APIRequestContext,
  path: string,
  headers: Record<string, string>,
): Promise<T> {
  const response = await request.get(`${apiBase}${path}`, { headers });
  expect(response.status(), `${path} status`).toBe(200);
  return await response.json() as T;
}

async function verifySignedRun(
  request: APIRequestContext,
  runId: string,
  headers: Record<string, string>,
): Promise<{ recordCount: number; evidence: RunEvidence }> {
  const verification = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
    headers,
    data: {},
  });
  expect(verification.status()).toBe(200);
  const chain = await verification.json() as {
    verified: boolean;
    signature_verified: boolean;
    unsigned_record_count: number;
    record_count: number;
  };
  expect(chain).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });
  expect(chain.record_count).toBeGreaterThan(0);
  const evidence = await getJson<RunEvidence>(request, `/v1/runs/${runId}/evidence`, headers);
  expect(evidence.audits).toHaveLength(chain.record_count);
  expect(evidence.audits.every((audit) => typeof audit.record_signature === "string")).toBe(true);
  return { recordCount: chain.record_count, evidence };
}

test.describe("provider-free subgraph partial failure", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(
    !apiKey || !parentWorkflowId || !parentDeploymentRef || !parentGraphVersion
      || !childDeploymentRef || !payloadText,
    "requires the controlled partial-failure fixture",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("preserves seven children and one explicit failure in best-effort order", async ({ page, request }, testInfo) => {
    test.setTimeout(90_000);
    coverCriteria(testInfo, "subgraphs.child-failure-and-partial-collection");
    const headers = { "X-API-Key": apiKey!, "X-Tenant-ID": tenant };
    const health = await getJson<Record<string, unknown>>(request, "/health", headers);
    expect(health).toMatchObject({
      status: "ok",
      deployment_ref: parentDeploymentRef,
      graph_version_ref: parentGraphVersion,
    });

    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(parentWorkflowId!)}`, {
      waitUntil: "networkidle",
    });
    await page.locator('.react-flow__node[data-id="batch-input"]').click();
    const batchDialog = page.getByRole("dialog", { name: "Edit Eight-item batch" });
    await batchDialog.getByRole("button", { name: "Execution" }).click();
    await expect(batchDialog.getByLabel("Parallel failure mode")).toHaveValue("best_effort");
    await page.keyboard.press("Escape");
    await page.locator('.react-flow__node[data-id="deterministic-child"]').click();
    const childDialog = page.getByRole("dialog", { name: "Edit Deterministic child" });
    await expect(childDialog.getByLabel("Graph ref")).toHaveValue(childDeploymentRef!);
    await page.keyboard.press("Escape");

    const dock = page.locator(".studio-run-dock");
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    await dock.getByRole("textbox", { name: /Input payload/ }).fill(payloadText!);
    await testInfo.attach("partial-failure-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    const submitted = page.waitForResponse((response) =>
      response.url().endsWith("/v1/runs") && response.request().method() === "POST"
    );
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    const submission = await submitted;
    expect(submission.status()).toBe(202);
    const created = await submission.json() as { run_id: string };
    let parent: RunStatus | null = null;
    await expect.poll(async () => {
      parent = await getJson<RunStatus>(request, `/v1/runs/${created.run_id}`, headers);
      return parent.status;
    }, { timeout: 45_000, intervals: [100, 250, 500] }).toBe("succeeded");

    const expected = (JSON.parse(payloadText!) as { items: Array<Record<string, unknown> | null> }).items;
    expected[3] = null;
    expect(parent!.terminal_output).toEqual({ items: expected });
    const childSummaries = await getJson<ChildSummary[]>(
      request,
      `/v1/runs/${created.run_id}/children`,
      headers,
    );
    expect(childSummaries).toHaveLength(8);
    const childRuns = await Promise.all(
      childSummaries.map((child) => getJson<RunStatus>(request, `/v1/runs/${child.run_id}`, headers)),
    );
    const ordered = childRuns
      .map((child) => ({ child, branch_index: branchIndex(child) }))
      .sort((left, right) => left.branch_index - right.branch_index);
    expect(ordered.map((item) => item.branch_index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(new Set(ordered.map((item) => item.child.thread_id)).size).toBe(8);
    expect(ordered.filter((item) => item.child.status === "succeeded")).toHaveLength(7);
    const failed = ordered.filter((item) => item.child.status === "failed");
    expect(failed).toHaveLength(1);
    expect(failed[0].branch_index).toBe(3);
    expect(failed[0].child.failure_state?.reason).toBe("node_execution_failed");

    const parentAudit = await verifySignedRun(request, parent!.run_id, headers);
    const childAudits = await Promise.all(
      ordered.map((item) => verifySignedRun(request, item.child.run_id, headers)),
    );
    expect(parentAudit.evidence.summary).toMatchObject({
      priced_call_count: 0,
      total_cost_usd: 0,
      cost_identity_state: "not_applicable_no_priced_call",
    });

    await page.goto(`/console/runs/?run=${encodeURIComponent(parent!.run_id)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText("Child runs (8)");
    await expect(page.getByText("Node timeline", { exact: true }).locator("..").locator(".z-pulse")).toHaveCount(0);
    await testInfo.attach("partial-failure-parent-result", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    const failedRunId = failed[0].child.run_id;
    await page.locator(`[data-evidence-id="runs.lineage.child.${failedRunId}"]`).click();
    await expect(page.locator('[data-evidence-id="runs.lineage.parent"]')).toContainText(parent!.run_id);
    await expect(
      page.getByLabel("Run details").getByText("failed", { exact: true }),
    ).toBeVisible();
    const failedTimeline = page.getByText("Node timeline", { exact: true }).locator("..");
    await expect(failedTimeline.locator(".z-pulse")).toHaveCount(0);
    await expect(failedTimeline.getByText("***REDACTED***", { exact: true })).toHaveCount(0);
    await expect(failedTimeline).toContainText("failed · executable unit execution error");
    await testInfo.attach("partial-failure-child-detail", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator('[data-evidence-id="runs.lineage.parent"]')).toContainText(parent!.run_id);
    await testInfo.attach("partial-failure-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "subgraph-partial-failure-summary", {
      schema_version: 1,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      parent: {
        identity: parent!.run_id,
        status: parent!.status,
        terminal_output: parent!.terminal_output,
      },
      children: ordered.map((item) => ({
        identity: item.child.run_id,
        thread_identity: item.child.thread_id,
        parent_identity: item.child.parent_run_id,
        status: item.child.status,
        branch_index: item.branch_index,
      })),
      failed_child: {
        identity: failedRunId,
        status: failed[0].child.status,
        failure_reason: failed[0].child.failure_state?.reason,
      },
      economics: parentAudit.evidence.summary,
      audit: {
        parent_record_count: parentAudit.recordCount,
        parent_audit_ids: parentAudit.evidence.audits.map((audit) => audit.audit_id),
        child_record_counts: childAudits.map((audit) => audit.recordCount),
        child_audit_ids: childAudits.map((audit) =>
          audit.evidence.audits.map((record) => record.audit_id)
        ),
        all_records_signed: true,
        chain_state: "chain_intact_signatures_valid",
      },
      economics_identities: {
        cost_event_ids: [],
        provider_request_ids: [],
        operation_ids: [],
        state: "not_applicable_no_priced_call",
      },
      provider_calls_performed: 0,
    });
  });
});
