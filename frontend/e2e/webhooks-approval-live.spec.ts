import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8124";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1-twin";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const proofPath = process.env.ZEROTH_EVALUATION_APPROVAL_WEBHOOK_PROOF;

type ProofEvent = {
  event_type: string;
  approval_id: string;
  run_id: string;
  thread_id: string;
};

type Proof = {
  campaign_id: string;
  tenant_id: string;
  deployment_ref: string;
  subscription_id: string;
  provider_calls: number;
  external_action_calls: number;
  delivery_transport: string;
  events: ProofEvent[];
  audit_verification: Record<string, {
    verified: boolean;
    signature_verified: boolean;
    unsigned_record_count: number;
  }>;
};

type Delivery = {
  event_type: string;
  run_id: string;
  approval_id: string;
};

test("approval webhook lifecycle is visible with safe correlation", async ({ page }, testInfo) => {
  test.skip(!liveEnabled, "requires the disposable local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  expect(proofPath, "ZEROTH_EVALUATION_APPROVAL_WEBHOOK_PROOF is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(
    testInfo,
    "webhooks.approval-requested-live",
    "webhooks.approval-resolved-live",
    "webhooks.approval-escalated-live",
    "webhooks.approval-event-unique",
    "webhooks.approval-safe-ui-correlation",
    "webhooks.approval-signed-audit",
    "webhooks.approval-provider-free",
  );

  const proof = JSON.parse(readFileSync(proofPath!, "utf8")) as Proof;
  expect(proof).toMatchObject({
    tenant_id: tenant,
    provider_calls: 0,
    external_action_calls: 0,
    delivery_transport: "campaign-local-evaluation-sink",
  });
  expect(proof.events).toHaveLength(4);
  for (const verification of Object.values(proof.audit_verification)) {
    expect(verification).toMatchObject({
      verified: true,
      signature_verified: true,
      unsigned_record_count: 0,
    });
  }

  await configurePage(page, apiBase, tenant, apiKey!);
  const evidence = new BrowserEvidence(page, new URL(apiBase).origin);
  await page.goto("/console/webhooks/", { waitUntil: "networkidle" });
  const deliveries: Delivery[] = [];
  for (const [index, expected] of proof.events.entries()) {
    const row = page.locator('[data-evidence-id^="webhooks.delivery."]').filter({
      hasText: expected.event_type,
    }).filter({
      hasText: `approval ${expected.approval_id}`,
    });
    await expect(row).toHaveCount(1);
    await expect(row).toBeVisible();
    const correlation = row.locator('[data-evidence-id$=".correlation"]');
    await expect(correlation).toContainText(
      `run ${expected.run_id} · approval ${expected.approval_id}`,
    );
    await expect(row).toContainText("delivered");
    await testInfo.attach(`approval-webhook-row-${index + 1}-${expected.event_type}`, {
      body: await row.screenshot({ animations: "disabled" }),
      contentType: "image/png",
    });
    deliveries.push({
      event_type: expected.event_type,
      run_id: expected.run_id,
      approval_id: expected.approval_id,
    });
  }
  await expect(page.locator("body")).not.toContainText("payload_json");
  await assertAccessibility(page, testInfo);
  evidence.assertNoFailedApiResponses();
  await attachSafeJson(testInfo, "approval-webhook-runtime-proof", proof);
  await attachSafeJson(testInfo, "approval-webhook-delivery-identities", deliveries);
  await testInfo.attach("approval-webhook-safe-correlation", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await evidence.attach(testInfo);
});
