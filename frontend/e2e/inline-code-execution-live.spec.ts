import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const workflowId = "2f2b20b2-8acc-4488-9a01-71b1b4f088f6";
const deploymentRef = "acceptance-inline-code-success-v1";

test.describe("published inline-code execution from Studio", () => {
  test.skip(!liveEnabled, "requires the persistent local evaluation service");

  test.beforeEach(async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
    expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, apiKey!);
  });

  test("submits sandboxed inline code and restores its signed result after refresh", async ({ page, request }, testInfo) => {
    test.setTimeout(45_000);
    coverCriteria(
      testInfo,
      "code.inline-studio-execution",
      "code.content-identity-live",
      "code.zero-provider-cost",
      "runs.inline-refresh-restoration",
    );
    const headers = { "X-API-Key": apiKey! };
    const health = await (await request.get(`${apiBase}/health`)).json() as {
      deployment_ref: string;
      graph_version_ref: string;
    };
    expect(health).toMatchObject({
      deployment_ref: deploymentRef,
      graph_version_ref: `${workflowId}@1`,
    });

    await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "domcontentloaded" });
    await expect(page.getByLabel("Workflow name")).toHaveValue("Acceptance inline code success 20260825");
    const dock = page.locator(".studio-run-dock");
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    const payload = dock.getByRole("textbox", { name: /Input payload/ });
    await expect(payload).toBeVisible();
    await payload.fill(JSON.stringify({
      records: [{ name: "Native UI", email: "ui@example.test", status: "new" }],
    }));
    const submitted = page.waitForResponse((response) =>
      response.url().endsWith("/v1/runs") && response.request().method() === "POST",
    );
    await dock.getByRole("button", { name: "Run", exact: true }).click();
    const submission = await submitted;
    expect(submission.status()).toBe(202);
    const { run_id: runId } = await submission.json() as { run_id: string };

    let run: {
      status: string;
      terminal_output: Record<string, unknown> | null;
      audit_refs: string[];
    } | null = null;
    await expect.poll(async () => {
      const response = await request.get(`${apiBase}/v1/runs/${runId}`, { headers });
      expect(response.status()).toBe(200);
      run = await response.json() as typeof run;
      return run?.status;
    }, { timeout: 20_000 }).toBe("succeeded");
    expect(run!.terminal_output).toMatchObject({ validated: true });
    await expect(dock.getByText(/^succeeded$/i)).toBeVisible({ timeout: 10_000 });
    await expect(dock.getByText(runId, { exact: true })).toBeVisible();

    const chainResponse = await request.post(`${apiBase}/v1/runs/${runId}/verify-chain`, {
      headers,
      data: {},
    });
    expect(chainResponse.status()).toBe(200);
    const chain = await chainResponse.json() as {
      verified: boolean;
      signature_verified: boolean;
      unsigned_record_count: number;
    };
    expect(chain).toMatchObject({
      verified: true,
      signature_verified: true,
      unsigned_record_count: 0,
    });
    const evidenceResponse = await request.get(`${apiBase}/v1/runs/${runId}/evidence`, { headers });
    expect(evidenceResponse.status()).toBe(200);
    const evidence = await evidenceResponse.json() as {
      audits: Array<{
        node_id: string;
        cost_usd: number | null;
        cost_measurement: string | null;
        execution_metadata: { manifest_ref_sha256?: string };
      }>;
      summary: {
        priced_call_count: number;
        cost_event_count: number;
        total_cost_usd: number;
        reconciliation_state: string;
      };
    };
    expect(evidence.summary).toMatchObject({
      priced_call_count: 0,
      cost_event_count: 0,
      total_cost_usd: 0,
      reconciliation_state: "reconciled_zero_activity",
    });
    const transform = evidence.audits.find((audit) => audit.node_id === "transform");
    expect(transform).toMatchObject({
      cost_usd: 0,
      cost_measurement: "measured",
      execution_metadata: {
        manifest_ref_sha256: "902fe694adcad10ec1062d683a3c1d06d6668542b8ad25a85dbf1e240408a01d",
      },
    });
    await attachSafeJson(testInfo, "inline-code-studio-runtime", {
      run_id: runId,
      run,
      chain,
      economics: evidence.summary,
      transform_identity: transform?.execution_metadata.manifest_ref_sha256,
    });
    await testInfo.attach("inline-code-studio-succeeded", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(dock.getByText(/^succeeded$/i)).toBeVisible({ timeout: 10_000 });
    await expect(dock.getByText(runId, { exact: true })).toBeVisible();
    await testInfo.attach("inline-code-studio-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
  });
});
