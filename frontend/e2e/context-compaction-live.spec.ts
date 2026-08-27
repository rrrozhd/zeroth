import { expect, test, type APIRequestContext } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_CONTEXT_CHECKPOINT_LIVE === "1";
const apiBase = process.env.ZEROTH_CONTEXT_CHECKPOINT_API_BASE ?? "http://127.0.0.1:8124";
const apiKey = process.env.ZEROTH_CONTEXT_CHECKPOINT_API_KEY;
const tenant = "evaluation-context-v1";

type Run = { run_id: string; thread_id: string; status: string };

async function waitForSuccess(request: APIRequestContext, runId: string): Promise<Run> {
  let run: Run | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${runId}`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    run = await response.json() as Run;
    return run.status;
  }, { timeout: 30_000, intervals: [200, 500, 1_000] }).toBe("succeeded");
  return run!;
}

function turns(label: string) {
  const long = Array.from({ length: 24 }, () => label).join(" ");
  return [
    { role: "human", content: long },
    { role: "ai", content: long },
    { role: "human", content: long },
  ];
}

test("compacted context is saved, continued, and restored in the Runs UI", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the provider-free context checkpoint service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_CONTEXT_CHECKPOINT_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(
    testInfo,
    "runtime_compaction_product_surface_evidence",
    "compacted_thread_state_save_refresh_reopen",
  );
  await configurePage(page, apiBase, tenant, apiKey!);

  const firstCreate = await request.post(`${apiBase}/v1/runs`, {
    headers: { "X-API-Key": apiKey! },
    data: { input_payload: { messages: turns("first-checkpoint") } },
  });
  expect(firstCreate.status()).toBe(202);
  const first = await waitForSuccess(request, (await firstCreate.json() as Run).run_id);

  const secondCreate = await request.post(`${apiBase}/v1/runs`, {
    headers: { "X-API-Key": apiKey! },
    data: {
      thread_id: first.thread_id,
      input_payload: { messages: turns("continuation-checkpoint") },
    },
  });
  expect(secondCreate.status()).toBe(202);
  const second = await waitForSuccess(request, (await secondCreate.json() as Run).run_id);
  expect(second.run_id).not.toBe(first.run_id);
  expect(second.thread_id).toBe(first.thread_id);

  const timelineResponse = await request.get(`${apiBase}/v1/runs/${second.run_id}/timeline`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(timelineResponse.status()).toBe(200);
  const timeline = await timelineResponse.json() as {
    entries: Array<{ audit_id: string; execution_metadata: Record<string, unknown> }>;
  };
  expect(timeline.entries).toHaveLength(1);
  const entry = timeline.entries[0];
  expect(entry.execution_metadata).toMatchObject({
    context_compaction_applied: true,
    context_compaction_strategy: "truncation",
    thread_state_checkpointed: true,
    compacted_thread_state_saved: true,
    cost_usd: 0,
  });
  expect(Number(entry.execution_metadata.context_tokens_after)).toBeLessThan(
    Number(entry.execution_metadata.context_tokens_before),
  );
  expect(Number(entry.execution_metadata.context_messages_after)).toBeLessThan(
    Number(entry.execution_metadata.context_messages_before),
  );
  const verificationResponse = await request.get(
    `${apiBase}/v1/runs/${second.run_id}/audit-verification`,
    { headers: { "X-API-Key": apiKey! } },
  );
  expect(verificationResponse.status()).toBe(200);
  const verification = await verificationResponse.json() as {
    verified: boolean;
    signature_verified: boolean;
    unsigned_record_count: number;
    signing_key_id: string | null;
  };
  expect(verification).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });

  await page.goto(`/console/runs/?run=${second.run_id}`, { waitUntil: "domcontentloaded" });
  const context = page.locator('[data-evidence-id="runs.context-window"]');
  await expect(context).toContainText("Context management", { timeout: 15_000 });
  await expect(context).toContainText("truncation");
  await expect(context).toContainText(`state saved to thread ${first.thread_id}`);
  const costSummary = page.locator('[data-evidence-id="runs.evidence.cost-summary"]');
  await expect(costSummary).toContainText("No priced calls");
  await expect(costSummary).toContainText("Cost identity not applicable");
  await expect(costSummary).toContainText("$0.0000 reconciled");
  await page.getByRole("button", { name: "Verify chain", exact: true }).click();
  await expect(page.getByText("chain intact · signatures valid (1 record)", { exact: true })).toBeVisible();
  await testInfo.attach("runtime-compaction-product-surface", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(context).toContainText(`state saved to thread ${first.thread_id}`);
  await page.goto("/console/runs/", { waitUntil: "domcontentloaded" });
  await page.goto(`/console/runs/?run=${second.run_id}`, { waitUntil: "domcontentloaded" });
  await expect(context).toContainText(`state saved to thread ${first.thread_id}`);
  await testInfo.attach("compacted-thread-refresh-reopen", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "context-compaction-runtime", {
    first_run_id: first.run_id,
    second_run_id: second.run_id,
    thread_id: first.thread_id,
    audit_id: entry.audit_id,
    deployment_ref: "evaluation-context-compaction-v1",
    graph_version_ref: "evaluation-context-compaction@1",
    compaction_strategy: "truncation",
    thread_state_checkpointed: true,
    compacted_thread_state_saved: true,
    audit_chain_verified: verification.verified,
    audit_signature_verified: verification.signature_verified,
    signing_key_id: verification.signing_key_id,
    priced_call_count: 0,
    cost_identity_state: "not_applicable_no_priced_call",
    reconciliation_state: "reconciled_zero_activity",
    provider_cost_usd: 0,
  });
});
