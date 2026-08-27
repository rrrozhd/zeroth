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
const parentWorkflow = process.env.ZEROTH_EVALUATION_W2_RETRIEVAL_PARENT_WORKFLOW_ID;
const parentDeployment = process.env.ZEROTH_EVALUATION_W2_RETRIEVAL_PARENT_DEPLOYMENT_REF;
const parentGraph = process.env.ZEROTH_EVALUATION_W2_RETRIEVAL_PARENT_GRAPH_VERSION;
const childDeployment = process.env.ZEROTH_EVALUATION_W2_RETRIEVAL_CHILD_DEPLOYMENT_REF;
const childDeploymentVersion = Number(
  process.env.ZEROTH_EVALUATION_W2_RETRIEVAL_CHILD_DEPLOYMENT_VERSION,
);

const items = Array.from({ length: 8 }, (_, index) => ({
  index,
  value: `deterministic-item-${index}`,
  query: `provider-free-workflow2-retrieval-${index}`,
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
  terminal_output: { items: Array<Record<string, unknown> | null> } | null;
  failure_state: { reason: string; message: string } | null;
  traversal: { node_visit_counts: Record<string, number> };
};

type AuditRecord = {
  audit_id: string;
  run_id: string;
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

type RunEvidence = {
  audits: AuditRecord[];
  summary: {
    priced_call_count: number;
    cost_event_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
    reconciliation_state: string;
  };
};

type VerifiedEvidence = {
  recordCount: number;
  unsignedRecordCount: number;
  evidence: RunEvidence;
};

type ChildProjection = {
  run_id: string;
  thread_id: string;
  parent_run_id: string;
  branch_index: number;
  status: string;
  failure_reason: string | null;
};

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${apiBase}${path}`, { headers: headers() });
  expect(response.status(), `${path} status`).toBe(200);
  return await response.json() as T;
}

async function waitForTerminal(
  request: APIRequestContext,
  runId: string,
): Promise<RunStatus> {
  let observed: RunStatus | null = null;
  await expect.poll(async () => {
    observed = await getJson<RunStatus>(request, `/v1/runs/${encodeURIComponent(runId)}`);
    return observed.status;
  }, { timeout: 60_000, intervals: [100, 250, 500] }).toBe("succeeded");
  return observed!;
}

function branchIndex(run: RunStatus): number {
  const match = Object.keys(run.traversal.node_visit_counts)
    .map((nodeId) => /^branch:(\d+):subgraph:/.exec(nodeId))
    .find((candidate) => candidate !== null);
  expect(match, `child ${run.run_id} must expose branch-qualified traversal`).not.toBeUndefined();
  return Number(match![1]);
}

function qualifiedChildNodeId(branch: number, nodeId: string): string {
  return `branch:${branch}:subgraph:${childDeployment}:${childDeploymentVersion}:${nodeId}`;
}

async function children(
  request: APIRequestContext,
  parentRunId: string,
): Promise<Array<{ run: RunStatus; projection: ChildProjection }>> {
  const summaries = await getJson<Array<{ run_id: string }>>(
    request,
    `/v1/runs/${encodeURIComponent(parentRunId)}/children`,
  );
  expect(summaries).toHaveLength(8);
  const runs = await Promise.all(
    summaries.map((child) =>
      getJson<RunStatus>(request, `/v1/runs/${encodeURIComponent(child.run_id)}`)
    ),
  );
  const ordered = runs.map((run) => ({
    run,
    projection: {
      run_id: run.run_id,
      thread_id: run.thread_id,
      parent_run_id: run.parent_run_id!,
      branch_index: branchIndex(run),
      status: run.status,
      failure_reason: run.failure_state?.reason ?? null,
    },
  })).sort((left, right) => left.projection.branch_index - right.projection.branch_index);
  expect(ordered.map((item) => item.projection.branch_index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  expect(new Set(ordered.map((item) => item.projection.run_id)).size).toBe(8);
  expect(new Set(ordered.map((item) => item.projection.thread_id)).size).toBe(8);
  expect(ordered.every((item) => item.projection.parent_run_id === parentRunId)).toBe(true);
  return ordered;
}

async function verifySignedZeroActivity(
  request: APIRequestContext,
  runId: string,
): Promise<VerifiedEvidence> {
  const verification = await request.post(
    `${apiBase}/v1/runs/${encodeURIComponent(runId)}/verify-chain`,
    { headers: headers(), data: {} },
  );
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
  const evidence = await getJson<RunEvidence>(
    request,
    `/v1/runs/${encodeURIComponent(runId)}/evidence`,
  );
  expect(evidence.audits).toHaveLength(chain.record_count);
  expect(evidence.audits.every((audit) => audit.run_id === runId)).toBe(true);
  expect(evidence.audits.every((audit) => typeof audit.record_signature === "string")).toBe(true);
  expect(evidence.audits.every((audit) => audit.cost_event_id === null)).toBe(true);
  expect(
    evidence.audits.every((audit) => audit.execution_metadata.provider_request_id == null),
  ).toBe(true);
  expect(evidence.summary).toMatchObject({
    priced_call_count: 0,
    cost_event_count: 0,
    total_cost_usd: 0,
    cost_identity_state: "not_applicable_no_priced_call",
    reconciliation_state: "reconciled_zero_activity",
  });
  return {
    recordCount: chain.record_count,
    unsignedRecordCount: chain.unsigned_record_count,
    evidence,
  };
}

async function visibleChildRunIds(page: Page): Promise<string[]> {
  const values = await page.locator('[data-evidence-id^="runs.lineage.child."]')
    .evaluateAll((elements) => elements.map((element) =>
      element.getAttribute("data-evidence-id")?.replace("runs.lineage.child.", "") ?? ""
    ));
  return values.filter(Boolean).sort();
}

async function submitFromStudio(page: Page, testInfo: TestInfo): Promise<string> {
  await page.goto(`/console/studio/edit/?id=${encodeURIComponent(parentWorkflow!)}`, {
    waitUntil: "networkidle",
  });
  await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
  const existing = page.getByRole("dialog", { name: "Run workflow" });
  if (await existing.isVisible()) {
    await existing.getByRole("button", { name: "Close run dialog" }).click();
    await expect(existing).toBeHidden();
  }
  await page.locator('.react-flow__node[data-id="batch-input"]').click();
  const batch = page.getByRole("dialog", { name: "Edit Eight retrieval investigations" });
  await batch.getByRole("button", { name: "Execution" }).click();
  await expect(batch.getByLabel("Maximum concurrency")).toHaveValue("4");
  await expect(batch.getByLabel("Batch size")).toHaveValue("8");
  await expect(batch.getByLabel("Parallel failure mode")).toHaveValue("best_effort");
  await page.keyboard.press("Escape");
  await page.locator('.react-flow__node[data-id="retrieval-child"]').click();
  const child = page.getByRole("dialog", { name: "Edit Investigate with local retrieval" });
  await expect(child.getByLabel("Graph ref")).toHaveValue(childDeployment!);
  await page.keyboard.press("Escape");

  await page.locator('[data-evidence-id="studio.run.open"]').click();
  const runDialog = page.getByRole("dialog", { name: "Run workflow" });
  await runDialog.getByRole("textbox", { name: /Input payload/ }).fill(payloadText);
  await testInfo.attach("01-exact-eight-retrieval-miss-configured", {
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

test.describe("Workflow 2 exact-eight retrieval miss", () => {
  test.skip(!liveEnabled, "requires the isolated persistent evaluation service");
  test.skip(
    !apiKey || !parentWorkflow || !parentDeployment || !parentGraph || !childDeployment
      || !Number.isInteger(childDeploymentVersion) || childDeploymentVersion < 1,
    "requires the exact provider-independent Workflow-2 retrieval-miss fixture",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("restores one zero-result retrieval failure with seven ordered successes", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(150_000);
    coverCriteria(
      testInfo,
      "workflow2.negative-retrieval-miss",
      "subgraphs.child-failure-and-partial-collection",
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

    const parentRunId = await submitFromStudio(page, testInfo);
    const parent = await waitForTerminal(request, parentRunId);
    const expectedOutput: Array<Record<string, unknown> | null> = items.map((item) => ({ ...item }));
    expectedOutput[3] = null;
    expect(parent).toMatchObject({
      run_id: parentRunId,
      parent_run_id: null,
      status: "succeeded",
      deployment_ref: parentDeployment,
      graph_version_ref: parentGraph,
      terminal_output: { items: expectedOutput },
    });
    const ordered = await children(request, parentRunId);
    expect(ordered.filter((item) => item.projection.status === "succeeded")).toHaveLength(7);
    const failed = ordered.filter((item) => item.projection.status === "failed");
    expect(failed).toHaveLength(1);
    expect(failed[0].projection).toMatchObject({
      branch_index: 3,
      failure_reason: "node_execution_failed",
    });

    const parentAudit = await verifySignedZeroActivity(request, parentRunId);
    const childAudits = await Promise.all(
      ordered.map((item) => verifySignedZeroActivity(request, item.projection.run_id)),
    );
    const retrievalNodeId = qualifiedChildNodeId(3, "local-retrieval");
    const missFailureNodeId = qualifiedChildNodeId(3, "require-retrieval-hit");
    const missAudit = childAudits[3].evidence.audits.find(
      (audit) => audit.node_id === retrievalNodeId,
    );
    expect(missAudit).toBeDefined();
    expect(missAudit).toMatchObject({
      status: "completed",
      execution_metadata: { retrieval_result_count: 0 },
    });
    expect(
      childAudits[3].evidence.audits.some(
        (audit) => audit.node_id === missFailureNodeId && audit.status === "failed",
      ),
    ).toBe(true);
    expect(
      childAudits.flatMap((evidence) =>
        evidence.evidence.audits.filter((audit) => audit.node_id === retrievalNodeId)
      ),
    ).toHaveLength(1);

    await page.goto(`/console/runs/?run=${encodeURIComponent(parentRunId)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText(
      "Child runs (8)",
    );
    const expectedChildIds = ordered.map((item) => item.projection.run_id).sort();
    const beforeChildIds = await visibleChildRunIds(page);
    expect(beforeChildIds).toEqual(expectedChildIds);
    await testInfo.attach("02-seven-successes-one-retrieval-failure", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const failedRunId = failed[0].projection.run_id;
    await page.locator(`[data-evidence-id="runs.lineage.child.${failedRunId}"]`).click();
    await expect(page.locator('[data-evidence-id="runs.lineage.parent"]')).toContainText(parentRunId);
    const timeline = page.getByText("Node timeline", { exact: true }).locator("..");
    await expect(timeline.locator(".z-pulse")).toHaveCount(0);
    await expect(timeline).toContainText(retrievalNodeId);
    await expect(timeline).toContainText("failed · executable unit execution error");
    await expect(timeline.getByText("***REDACTED***", { exact: true })).toHaveCount(0);
    await testInfo.attach("03-zero-result-retrieval-child-detail", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator('[data-evidence-id="runs.lineage.parent"]')).toContainText(parentRunId);
    await testInfo.attach("04-failed-child-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.goto(`/console/runs/?run=${encodeURIComponent(parentRunId)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.locator('[data-evidence-id="runs.lineage.children"]')).toContainText(
      "Child runs (8)",
    );
    const restoredChildIds = await visibleChildRunIds(page);
    expect(restoredChildIds).toEqual(expectedChildIds);

    const allAudits = [parentAudit, ...childAudits].flatMap((proof) => proof.evidence.audits);
    const providerRequestIds = allAudits.flatMap((audit) =>
      audit.execution_metadata.provider_request_id ? [audit.execution_metadata.provider_request_id] : []
    );
    const costEventIds = allAudits.flatMap((audit) => audit.cost_event_id ? [audit.cost_event_id] : []);
    expect(providerRequestIds).toEqual([]);
    expect(costEventIds).toEqual([]);
    browserEvidence.assertNoFailedApiResponses();
    await browserEvidence.attach(testInfo);
    await attachSafeJson(testInfo, "workflow2-retrieval-miss-summary", {
      schema_version: 1,
      provider_calls_performed: 0,
      provider_request_ids: providerRequestIds,
      cost_event_ids: costEventIds,
      priced_call_count: 0,
      total_cost_usd: 0,
      configured_max_concurrency: 4,
      retrieval_miss_branch_index: 3,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      parent: {
        run_id: parent.run_id,
        thread_id: parent.thread_id,
        status: parent.status,
        terminal_output: parent.terminal_output,
      },
      children: ordered.map((item) => item.projection),
      retrieval_miss: {
        child_run_id: failedRunId,
        retrieval_node_id: retrievalNodeId,
        retrieval_result_count: missAudit!.execution_metadata.retrieval_result_count,
        failure_node_id: missFailureNodeId,
        failure_reason: failed[0].projection.failure_reason,
      },
      refresh: {
        before_parent_run_id: parentRunId,
        restored_parent_run_id: parentRunId,
        before_child_run_ids: beforeChildIds,
        restored_child_run_ids: restoredChildIds,
      },
      audit: {
        signed_parent_chain: true,
        signed_child_chain_count: 8,
        unsigned_record_count: parentAudit.unsignedRecordCount
          + childAudits.reduce((total, proof) => total + proof.unsignedRecordCount, 0),
        parent_run_id: parentRunId,
        parent_audit_ids: parentAudit.evidence.audits.map((audit) => audit.audit_id),
        child_parent_links: ordered.map((item, index) => ({
          child_run_id: item.projection.run_id,
          parent_run_id: item.projection.parent_run_id,
          child_audit_ids: childAudits[index].evidence.audits.map((audit) => audit.audit_id),
        })),
      },
    });
  });
});
