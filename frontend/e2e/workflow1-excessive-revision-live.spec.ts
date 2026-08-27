import { expect, test, type APIRequestContext } from "@playwright/test";

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
const workflowId = process.env.ZEROTH_EVALUATION_W1_REVISION_WORKFLOW_ID;
const deploymentRef = process.env.ZEROTH_EVALUATION_W1_REVISION_DEPLOYMENT_REF;
const graphVersionRef = process.env.ZEROTH_EVALUATION_W1_REVISION_GRAPH_VERSION;
const payloadText = process.env.ZEROTH_EVALUATION_W1_REVISION_PAYLOAD
  ?? JSON.stringify({ query: "synthetic-excessive-revision" }, null, 2);

const headers = () => ({ "X-API-Key": apiKey!, "X-Tenant-ID": tenant });

type RunStatus = {
  run_id: string;
  thread_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  failure_state: { reason: string; message: string } | null;
  traversal: { node_visit_counts: Record<string, number> };
};

type Audit = {
  audit_id: string;
  node_id: string;
  status: string;
  record_signature: string | null;
  cost_event_id: string | null;
  cost_usd: number | null;
  execution_metadata: { provider_request_id?: string | null };
};

type RunEvidence = {
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
  expect(response.status(), `${path} status`).toBe(200);
  return await response.json() as T;
}

test.describe("Workflow 1 provider-independent excessive revision", () => {
  test.skip(!liveEnabled, "requires an isolated persistent evaluation service");
  test.skip(
    !apiKey || !workflowId || !deploymentRef || !graphVersionRef,
    "requires the exact provider-independent excessive-revision fixture",
  );

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("stops after exactly two research visits and restores the terminal run after refresh", async ({
    page,
    request,
  }, testInfo) => {
    test.setTimeout(90_000);
    coverCriteria(testInfo, "workflow1.negative-excessive-revision");
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    let pageErrorCount = 0;
    page.on("pageerror", () => { pageErrorCount += 1; });

    const health = await getJson<Record<string, unknown>>(request, "/health");
    expect(health).toMatchObject({
      status: "ok",
      campaign_id: tenant,
      deployment_ref: deploymentRef,
      graph_version_ref: graphVersionRef,
    });
    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, {
      waitUntil: "networkidle",
    });
    await expect(page.getByLabel("Workflow graph editor")).toBeVisible();
    await page.locator('.react-flow__node[data-id="revision-loop"]').click();
    const inspector = page.getByRole("dialog", { name: "Edit Revision loop guard" });
    await expect(inspector.getByLabel(/Done condition/)).toHaveValue(
      "payload.revision_required != True",
    );
    await testInfo.attach("01-exact-revision-loop-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.keyboard.press("Escape");

    await page.locator('[data-evidence-id="studio.run.open"]').click();
    const runDialog = page.getByRole("dialog", { name: "Run workflow" });
    await runDialog.getByRole("textbox", { name: /Input payload/ }).fill(payloadText);
    const submitted = page.waitForResponse((response) =>
      response.url().endsWith("/v1/runs") && response.request().method() === "POST"
    );
    await runDialog.getByRole("button", { name: "Run", exact: true }).click();
    const response = await submitted;
    expect(response.status()).toBe(202);
    const runId = (await response.json() as { run_id: string }).run_id;

    let run: RunStatus | null = null;
    await expect.poll(async () => {
      run = await getJson<RunStatus>(request, `/v1/runs/${encodeURIComponent(runId)}`);
      return run.status;
    }, { timeout: 45_000, intervals: [100, 250, 500] }).toBe("terminated_by_loop_guard");
    expect(run!).toMatchObject({
      run_id: runId,
      thread_id: runId,
      deployment_ref: deploymentRef,
      graph_version_ref: graphVersionRef,
      failure_state: { reason: "max_total_steps" },
      traversal: {
        node_visit_counts: { request: 1, research: 2, "revision-loop": 1 },
      },
    });

    const timeline = await getJson<{ entries: Audit[] }>(
      request,
      `/v1/runs/${encodeURIComponent(runId)}/timeline`,
    );
    expect(timeline.entries.map((entry) => entry.node_id)).toEqual([
      "request",
      "research",
      "revision-loop",
      "research",
    ]);
    const chainResponse = await request.post(
      `${apiBase}/v1/runs/${encodeURIComponent(runId)}/verify-chain`,
      { headers: headers(), data: {} },
    );
    expect(chainResponse.status()).toBe(200);
    const chain = await chainResponse.json() as {
      verified: boolean;
      signature_verified: boolean;
      record_count: number;
      unsigned_record_count: number;
    };
    expect(chain).toMatchObject({
      verified: true,
      signature_verified: true,
      record_count: 4,
      unsigned_record_count: 0,
    });
    const evidence = await getJson<RunEvidence>(
      request,
      `/v1/runs/${encodeURIComponent(runId)}/evidence`,
    );
    expect(evidence.audits.map((audit) => audit.node_id)).toEqual(
      timeline.entries.map((entry) => entry.node_id),
    );
    expect(evidence.audits.filter((audit) => audit.node_id === "research")).toHaveLength(2);
    expect(evidence.audits.every((audit) => typeof audit.record_signature === "string")).toBe(true);
    const providerRequestIds = evidence.audits.flatMap((audit) =>
      audit.execution_metadata.provider_request_id ? [audit.execution_metadata.provider_request_id] : []
    );
    const costEventIds = evidence.audits.flatMap((audit) =>
      audit.cost_event_id ? [audit.cost_event_id] : []
    );
    expect(providerRequestIds).toEqual([]);
    expect(costEventIds).toEqual([]);
    expect(evidence.summary).toMatchObject({
      priced_call_count: 0,
      cost_event_count: 0,
      total_cost_usd: 0,
      cost_identity_state: "not_applicable_no_priced_call",
      reconciliation_state: "reconciled_zero_activity",
    });

    await page.goto(`/console/runs/?run=${encodeURIComponent(runId)}`, {
      waitUntil: "networkidle",
    });
    const terminalRun = page.getByRole("button", {
      name: new RegExp(`${runId} terminated by loop guard`, "i"),
    });
    await expect(terminalRun).toBeVisible();
    await expect(page.getByText("reason: max_total_steps", { exact: false })).toBeVisible();
    const visits = page.getByRole("region", { name: "Node visit counts" });
    await expect(visits).toContainText("research");
    await expect(visits).toContainText("2 visits");
    await page.getByRole("button", { name: "Verify chain" }).click();
    await expect(page.getByText("chain intact · signatures valid (4 records)", {
      exact: true,
    })).toBeVisible();
    await testInfo.attach("02-exact-two-research-visits-loop-guard", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByRole("button", {
      name: new RegExp(`${runId} terminated by loop guard`, "i"),
    })).toBeVisible();
    await expect(page.getByText("reason: max_total_steps", { exact: false })).toBeVisible();
    await expect(page.getByRole("region", { name: "Node visit counts" })).toContainText("2 visits");
    await page.getByRole("button", { name: "Verify chain" }).click();
    await expect(page.getByText("chain intact · signatures valid (4 records)", {
      exact: true,
    })).toBeVisible();
    await testInfo.attach("03-loop-guard-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await attachSafeJson(testInfo, "workflow1-excessive-revision-summary", {
      schema_version: 1,
      health: {
        status: health.status,
        campaign_id: health.campaign_id,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      run: {
        run_id: run!.run_id,
        thread_id: run!.thread_id,
        status: run!.status,
        deployment_ref: run!.deployment_ref,
        graph_version_ref: run!.graph_version_ref,
        failure_reason: run!.failure_state!.reason,
        research_visit_count: run!.traversal.node_visit_counts.research,
        node_visit_counts: run!.traversal.node_visit_counts,
      },
      timeline: {
        node_ids: timeline.entries.map((entry) => entry.node_id),
        research_visit_count: timeline.entries.filter((entry) => entry.node_id === "research").length,
      },
      audit: {
        verified: chain.verified,
        signature_verified: chain.signature_verified,
        record_count: chain.record_count,
        unsigned_record_count: chain.unsigned_record_count,
        audit_ids: evidence.audits.map((audit) => audit.audit_id),
        research_audit_ids: evidence.audits
          .filter((audit) => audit.node_id === "research")
          .map((audit) => audit.audit_id),
      },
      economics: {
        provider_calls_performed: 0,
        provider_request_ids: providerRequestIds,
        cost_event_ids: costEventIds,
        ...evidence.summary,
      },
      refresh: {
        before_run_id: runId,
        restored_run_id: runId,
        restored_status: run!.status,
        restored_failure_reason: run!.failure_state!.reason,
        restored_research_visit_count: run!.traversal.node_visit_counts.research,
      },
    });
    browserEvidence.assertNoFailedApiResponses();
    expect(pageErrorCount).toBe(0);
    await browserEvidence.attach(testInfo);
  });
});
