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
const mode = process.env.ZEROTH_EVALUATION_BATCH_NEGATIVE_MODE;
const workflowId = process.env.ZEROTH_EVALUATION_BATCH_NEGATIVE_WORKFLOW_ID;
const deploymentRef = process.env.ZEROTH_EVALUATION_BATCH_NEGATIVE_DEPLOYMENT_REF;
const graphVersionRef = process.env.ZEROTH_EVALUATION_BATCH_NEGATIVE_GRAPH_VERSION;
const payloadText = process.env.ZEROTH_EVALUATION_BATCH_NEGATIVE_PAYLOAD;

type RunStatus = {
  run_id: string;
  status: string;
  deployment_ref: string;
  graph_version_ref: string;
  thread_id: string;
  parent_run_id: string | null;
  failure_state: { reason: string; message: string } | null;
};

type RunList = { runs: RunStatus[]; total: number };

type TenantCost = {
  total_cost_usd: number;
  actual_spend_usd: number;
  paid_spend_usd: number;
  estimated_spend_usd: number;
  unmeasured_spend_usd: number;
  active_exposure_usd: number;
  ambiguous_exposure_usd: number;
  budget_consumed_usd: number;
};

type RunEvidence = {
  audits: Array<{ audit_id: string; record_signature: string | null }>;
  summary: {
    audit_count: number;
    priced_call_count: number;
    total_cost_usd: number;
    cost_identity_state: string;
  };
};

const headers = (): Record<string, string> => ({
  "X-API-Key": apiKey!,
  "X-Tenant-ID": tenant,
});

async function getJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${apiBase}${path}`, { headers: headers() });
  expect(response.status(), `${path} status`).toBe(200);
  return await response.json() as T;
}

async function verifyChain(request: APIRequestContext, runId: string): Promise<RunEvidence> {
  const verification = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
    headers: headers(),
    data: {},
  });
  expect(verification.status()).toBe(200);
  expect(await verification.json()).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });
  const evidence = await getJson<RunEvidence>(request, `/v1/runs/${runId}/evidence`);
  expect(evidence.audits).toHaveLength(evidence.summary.audit_count);
  expect(evidence.audits.every((record) => typeof record.record_signature === "string")).toBe(true);
  return evidence;
}

test.describe("provider-independent batch negative matrix", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(!apiKey || !workflowId || !deploymentRef || !graphVersionRef, "requires an exact served fixture");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("rejects empty, over-24, and malformed batches before creating a run", async ({
    page,
    request,
  }, testInfo) => {
    test.skip(mode !== "contract", "contract phase only");
    test.setTimeout(90_000);
    coverCriteria(
      testInfo,
      "workflow2.negative-empty-batch",
      "workflow2.negative-over-24-batch",
      "batching.malformed-item",
    );
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    const unexpectedApiFailures: Array<{ method: string; path: string; status: number }> = [];
    let expectedConsoleErrorCount = 0;
    let unexpectedConsoleErrorCount = 0;
    let pageErrorCount = 0;
    page.on("response", (response) => {
      const url = new URL(response.url());
      const method = response.request().method();
      const expectedRejection = url.origin === new URL(apiBase).origin
        && url.pathname === "/v1/runs"
        && method === "POST"
        && response.status() === 422;
      if (url.origin === new URL(apiBase).origin && response.status() >= 400 && !expectedRejection) {
        unexpectedApiFailures.push({ method, path: url.pathname, status: response.status() });
      }
    });
    page.on("console", (message) => {
      if (message.type() !== "error") return;
      const location = message.location().url;
      const isExpectedValidationError = (() => {
        try {
          const url = new URL(location);
          return url.origin === new URL(apiBase).origin && url.pathname === "/v1/runs";
        } catch {
          return /\b422\b/.test(message.text());
        }
      })();
      if (isExpectedValidationError) expectedConsoleErrorCount += 1;
      else unexpectedConsoleErrorCount += 1;
    });
    page.on("pageerror", () => {
      pageErrorCount += 1;
    });
    const health = await getJson<Record<string, unknown>>(request, "/health");
    expect(health).toMatchObject({
      status: "ok",
      deployment_ref: deploymentRef,
      graph_version_ref: graphVersionRef,
    });
    const beforeRuns = await getJson<RunList>(request, "/v1/admin/runs?limit=1000");
    const beforeCost = await getJson<TenantCost>(request, `/v1/tenants/${tenant}/cost`);
    const cases = [
      {
        id: "empty",
        payload: { items: [] },
        type: "too_short",
      },
      {
        id: "over-24",
        payload: {
          items: Array.from({ length: 25 }, (_, index) => ({
            index,
            query: `provider-independent boundary item ${index}`,
          })),
        },
        type: "too_long",
      },
      {
        id: "malformed-item",
        payload: { items: [{ index: 0 }] },
        type: "missing",
      },
    ] as const;

    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, {
      waitUntil: "networkidle",
    });
    const dock = page.locator(".studio-run-dock");
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    const runDialog = page.getByRole("dialog", { name: "Run workflow" });
    const input = runDialog.getByRole("textbox", { name: /Input payload/ });
    const submit = runDialog.getByRole("button", { name: "Run", exact: true });
    const observations: Array<{ id: string; status: number; validation_type: string }> = [];
    for (const validationCase of cases) {
      await input.fill(JSON.stringify(validationCase.payload, null, 2));
      await testInfo.attach(`batch-${validationCase.id}-configured`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      const submitted = page.waitForResponse((response) => (
        response.url().endsWith("/v1/runs") && response.request().method() === "POST"
      ));
      await submit.click();
      const response = await submitted;
      expect(response.status()).toBe(422);
      const body = await response.json() as { detail: Array<{ type: string }> };
      expect(body.detail.some((detail) => detail.type === validationCase.type)).toBe(true);
      await expect(runDialog.getByText(new RegExp(validationCase.type))).toBeVisible();
      await testInfo.attach(`batch-${validationCase.id}-rejected`, {
        body: await page.screenshot({ fullPage: true, animations: "disabled" }),
        contentType: "image/png",
      });
      observations.push({
        id: validationCase.id,
        status: response.status(),
        validation_type: validationCase.type,
      });
    }

    const afterRuns = await getJson<RunList>(request, "/v1/admin/runs?limit=1000");
    const afterCost = await getJson<TenantCost>(request, `/v1/tenants/${tenant}/cost`);
    expect(afterRuns.total).toBe(beforeRuns.total);
    expect(afterRuns.runs.map((run) => run.run_id)).toEqual(beforeRuns.runs.map((run) => run.run_id));
    expect(afterCost).toEqual(beforeCost);
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator('[data-evidence-id="studio.run.current-id"]')).toHaveCount(0);
    await testInfo.attach("batch-contract-rejections-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "batch-contract-rejection-summary", {
      schema_version: 1,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      observations,
      run_count_before: beforeRuns.total,
      run_count_after: afterRuns.total,
      run_identities_unchanged: true,
      tenant_cost_unchanged: true,
      expected_validation_console_errors: expectedConsoleErrorCount,
      unexpected_console_errors: unexpectedConsoleErrorCount,
      page_errors: pageErrorCount,
      provider_calls_performed: 0,
    });
    expect(unexpectedApiFailures).toEqual([]);
    expect({ expectedConsoleErrorCount, unexpectedConsoleErrorCount, pageErrorCount }).toEqual({
      expectedConsoleErrorCount: cases.length,
      unexpectedConsoleErrorCount: 0,
      pageErrorCount: 0,
    });
    await browserEvidence.attach(testInfo);
  });

  test("restores an active batch after refresh and cancels a second batch without replay", async ({
    page,
    request,
  }, testInfo) => {
    test.skip(mode !== "runtime", "runtime phase only");
    expect(payloadText, "runtime phase requires the provider-free payload").toBeTruthy();
    test.setTimeout(150_000);
    coverCriteria(testInfo, "batching.active-refresh-restoration", "runs.cancel");
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    let consoleErrorCount = 0;
    let pageErrorCount = 0;
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrorCount += 1;
    });
    page.on("pageerror", () => {
      pageErrorCount += 1;
    });
    const health = await getJson<Record<string, unknown>>(request, "/health");
    expect(health).toMatchObject({
      status: "ok",
      deployment_ref: deploymentRef,
      graph_version_ref: graphVersionRef,
    });
    await page.goto(`/console/studio/edit/?id=${encodeURIComponent(workflowId!)}`, {
      waitUntil: "networkidle",
    });
    const dock = page.locator(".studio-run-dock");
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    const runDialog = page.getByRole("dialog", { name: "Run workflow" });
    await runDialog.getByRole("textbox", { name: /Input payload/ }).fill(payloadText!);

    const firstSubmitted = page.waitForResponse((response) => (
      response.url().endsWith("/v1/runs") && response.request().method() === "POST"
    ));
    await runDialog.getByRole("button", { name: "Run", exact: true }).click();
    const firstResponse = await firstSubmitted;
    expect(firstResponse.status()).toBe(202);
    const firstRunId = (await firstResponse.json() as { run_id: string }).run_id;
    await expect.poll(async () => (
      await getJson<RunStatus>(request, `/v1/runs/${firstRunId}`)
    ).status, { intervals: [100, 250], timeout: 10_000 }).toBe("running");
    await testInfo.attach("batch-active-before-refresh", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.reload({ waitUntil: "networkidle" });
    const restoredDialog = page.getByRole("dialog", { name: "Run workflow" });
    await expect(restoredDialog).toBeVisible();
    await expect(restoredDialog.locator('[data-evidence-id="studio.run.current-id"]')).toContainText(firstRunId);
    await testInfo.attach("batch-active-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await expect.poll(async () => (
      await getJson<RunStatus>(request, `/v1/runs/${firstRunId}`)
    ).status, { intervals: [250, 500], timeout: 45_000 }).toBe("succeeded");
    const firstEvidence = await verifyChain(request, firstRunId);
    expect(firstEvidence.summary).toMatchObject({
      priced_call_count: 0,
      total_cost_usd: 0,
      cost_identity_state: "not_applicable_no_priced_call",
    });

    await restoredDialog.getByRole("button", { name: "Clear" }).click();
    const secondSubmitted = page.waitForResponse((response) => (
      response.url().endsWith("/v1/runs") && response.request().method() === "POST"
    ));
    await restoredDialog.getByRole("button", { name: "Run", exact: true }).click();
    const secondResponse = await secondSubmitted;
    expect(secondResponse.status()).toBe(202);
    const secondRunId = (await secondResponse.json() as { run_id: string }).run_id;
    await expect.poll(async () => (
      await getJson<RunStatus>(request, `/v1/runs/${secondRunId}`)
    ).status, { intervals: [100, 250], timeout: 10_000 }).toBe("running");
    await expect.poll(async () => {
      const observedChildren = await getJson<RunStatus[]>(
        request,
        `/v1/runs/${secondRunId}/children`,
      );
      return observedChildren.some((child) => child.status === "running");
    }, { intervals: [100, 250], timeout: 10_000 }).toBe(true);
    await page.goto(`/console/runs/?run=${encodeURIComponent(secondRunId)}`, {
      waitUntil: "domcontentloaded",
    });
    const cancel = page.locator(`[data-evidence-id="runs.action.${secondRunId}.cancel"]`);
    await expect(cancel).toBeVisible();
    await testInfo.attach("batch-cancel-configured", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    page.once("dialog", (dialog) => dialog.accept());
    await cancel.click();
    await expect(page.getByText(`Cancelled ${secondRunId}`)).toBeVisible();
    let cancelled: RunStatus | null = null;
    await expect.poll(async () => {
      cancelled = await getJson<RunStatus>(request, `/v1/runs/${secondRunId}`);
      return cancelled.status;
    }, { intervals: [100, 250, 500], timeout: 30_000 }).toBe("failed");
    expect(cancelled!.failure_state?.reason).toBe("operator_cancelled");
    const children = await getJson<RunStatus[]>(request, `/v1/runs/${secondRunId}/children`);
    expect(children.length).toBeGreaterThan(0);
    expect(children.every((child) => !["queued", "running", "paused_for_approval"].includes(child.status))).toBe(true);
    expect(new Set(children.map((child) => child.run_id)).size).toBe(children.length);
    const cancelledEvidence = await verifyChain(request, secondRunId);
    expect(cancelledEvidence.summary).toMatchObject({
      priced_call_count: 0,
      total_cost_usd: 0,
      cost_identity_state: "not_applicable_no_priced_call",
    });
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText(/reason: operator_cancelled/)).toBeVisible();
    const afterRefreshChildren = await getJson<RunStatus[]>(
      request,
      `/v1/runs/${secondRunId}/children`,
    );
    expect(afterRefreshChildren.map((child) => child.run_id)).toEqual(children.map((child) => child.run_id));
    await testInfo.attach("batch-cancelled-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "batch-runtime-negative-summary", {
      schema_version: 1,
      health: {
        status: health.status,
        deployment_ref: health.deployment_ref,
        graph_version_ref: health.graph_version_ref,
      },
      active_refresh: {
        run_id: firstRunId,
        terminal_status: "succeeded",
        restored_while_active: true,
        audit_count: firstEvidence.summary.audit_count,
      },
      cancellation: {
        run_id: secondRunId,
        terminal_status: cancelled!.status,
        failure_reason: cancelled!.failure_state?.reason,
        child_count: children.length,
        child_statuses: children.map((child) => child.status),
        child_identities_stable_after_refresh: true,
        audit_count: cancelledEvidence.summary.audit_count,
      },
      provider_calls_performed: 0,
    });
    browserEvidence.assertNoFailedApiResponses();
    expect({ consoleErrorCount, pageErrorCount }).toEqual({
      consoleErrorCount: 0,
      pageErrorCount: 0,
    });
    await browserEvidence.attach(testInfo);
  });
});
