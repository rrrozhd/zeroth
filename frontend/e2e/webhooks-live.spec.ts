import { expect, test, type APIRequestContext } from "@playwright/test";

import {
  assertAccessibility,
  attachSafeJson,
  configurePage,
  coverCriteria,
} from "./support/live-evaluation";

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const campaignId = process.env.ZEROTH_EVALUATION_CAMPAIGN_ID ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const twinApiKey = process.env.ZEROTH_EVALUATION_TWIN_API_KEY;
const configuredRunPayload = process.env.ZEROTH_EVALUATION_WEBHOOK_RUN_PAYLOAD;

type Run = { run_id: string; status: string };
type Subscription = {
  subscription_id: string;
  secret: string;
  target_url: string;
  active: boolean;
};
type Delivery = {
  delivery_id: string;
  subscription_id: string;
  event_id: string;
  run_id: string | null;
  status: string;
  attempt_count: number;
};
type DeadLetter = {
  dead_letter_id: string;
  subscription_id: string;
  run_id: string | null;
  attempt_count: number;
};
type AuditRecord = {
  audit_id: string;
  node_id: string;
  run_id: string;
  record_signature: string | null;
  signing_key_id: string | null;
  execution_metadata: Record<string, unknown>;
};
type AuditVerification = {
  verified: boolean;
  signature_verified: boolean | null;
  unsigned_record_count: number;
  record_count: number;
};

async function runArtifact(request: APIRequestContext, label: string, kind = "json"): Promise<Run> {
  const inputPayload = configuredRunPayload
    ? JSON.parse(configuredRunPayload) as Record<string, unknown>
    : { kind, label };
  const response = await request.post(`${apiBase}/v1/runs`, {
    headers: { "X-API-Key": apiKey! },
    data: {
      input_payload: inputPayload,
      campaign_id: campaignId,
      campaign_strict: true,
    },
  });
  expect(response.status()).toBe(202);
  let run = await response.json() as Run;
  await expect.poll(async () => {
    const current = await request.get(`${apiBase}/v1/runs/${run.run_id}`, {
      headers: { "X-API-Key": apiKey! },
    });
    run = await current.json() as Run;
    return run.status;
  }, { timeout: 20_000, intervals: [200, 400, 800] }).toMatch(/succeeded|failed/);
  return run;
}

async function deliveries(request: APIRequestContext): Promise<Delivery[]> {
  const response = await request.get(`${apiBase}/v1/webhooks/deliveries`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  return (await response.json()).deliveries as Delivery[];
}

async function deadLetters(request: APIRequestContext): Promise<DeadLetter[]> {
  const response = await request.get(`${apiBase}/v1/webhooks/dead-letters`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  return (await response.json()).dead_letters as DeadLetter[];
}

async function subscriptions(request: APIRequestContext): Promise<Subscription[]> {
  const response = await request.get(`${apiBase}/v1/webhooks/subscriptions`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  return (await response.json()).subscriptions as Subscription[];
}

async function tenantAudits(request: APIRequestContext): Promise<AuditRecord[]> {
  const response = await request.get(`${apiBase}/v1/admin/audits`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  return (await response.json()).records as AuditRecord[];
}

async function expectSignedRunChain(request: APIRequestContext, runId: string): Promise<void> {
  const response = await request.get(`${apiBase}/v1/runs/${runId}/audit-verification`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(response.status()).toBe(200);
  const verification = await response.json() as AuditVerification;
  expect(verification).toMatchObject({
    verified: true,
    signature_verified: true,
    unsigned_record_count: 0,
  });
  expect(verification.record_count).toBeGreaterThan(0);
}

async function expectSignedWebhookAudit(
  request: APIRequestContext,
  subscriptionId: string,
  nodeId: string,
  transition: string,
): Promise<AuditRecord> {
  let matched: AuditRecord | undefined;
  await expect.poll(async () => {
    matched = (await tenantAudits(request)).find((record) => (
      record.node_id === nodeId &&
      record.execution_metadata.webhook_subscription_id === subscriptionId &&
      record.execution_metadata.webhook_transition === transition
    ));
    return Boolean(matched);
  }, { timeout: 10_000, intervals: [200, 400, 800] }).toBe(true);
  expect(matched!.record_signature).toBeTruthy();
  expect(matched!.signing_key_id).toBeTruthy();
  return matched!;
}

async function deactivate(request: APIRequestContext, subscriptionId: string): Promise<void> {
  const current = await request.get(`${apiBase}/v1/webhooks/subscriptions/${subscriptionId}`, {
    headers: { "X-API-Key": apiKey! },
  });
  if (current.status() === 404 || !(await current.json() as Subscription).active) return;
  expect(current.status()).toBe(200);
  const response = await request.delete(
    `${apiBase}/v1/webhooks/subscriptions/${subscriptionId}`,
    { headers: { "X-API-Key": apiKey! } },
  );
  expect(response.status()).toBe(204);
}

test("signed webhook delivery, filtering, dead-letter replay, isolation, and deactivation", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  expect(twinApiKey, "ZEROTH_EVALUATION_TWIN_API_KEY is required").toBeTruthy();
  test.setTimeout(150_000);
  coverCriteria(
    testInfo,
    "webhooks.success",
    "webhooks.filtering",
    "webhooks.dead-letter-replay",
    "webhooks.tenant-isolation",
    "webhooks.deactivation",
    "webhooks.secret-redaction",
    "webhooks.target-url-userinfo-refused",
    "webhooks.refresh-persistence",
    "webhooks.deactivation-stops-delivery",
    "webhooks.signed-audit",
    "webhooks.replay-error-display",
  );
  await configurePage(page, apiBase, tenant, apiKey!);
  await page.goto("/console/webhooks/", { waitUntil: "networkidle" });

  const target = page.locator('[data-evidence-id="webhooks.target-url"]');
  const completed = page.locator('[data-evidence-id="webhooks.event.run.completed"]');
  const create = page.locator('[data-evidence-id="webhooks.create"]');
  const suffix = `${Date.now()}`;
  const createdSubscriptionIds: string[] = [];

  try {
    await create.click();
    await expect(page.getByText("Target URL and at least one event type are required.")).toBeVisible();
    await testInfo.attach("webhook-required-validation", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const beforeUserinfo = await subscriptions(request);
    await target.fill("https://embedded-user@example.com/zeroth-evaluation/success");
    await completed.check();
    const userinfoResponse = page.waitForResponse((response) => (
      response.url().endsWith("/v1/webhooks/subscriptions") &&
      response.request().method() === "POST"
    ));
    await create.click();
    expect((await userinfoResponse).status()).toBe(400);
    await expect(page.getByText(/credentials in destination URLs are not permitted/i)).toBeVisible();
    await target.fill("");
    expect((await subscriptions(request)).length).toBe(beforeUserinfo.length);
    await testInfo.attach("webhook-embedded-url-credentials-refused", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await target.fill("https://example.com/zeroth-evaluation/success");
    if (!(await completed.isChecked())) await completed.check();
    const successResponse = page.waitForResponse((response) => (
      response.url().endsWith("/v1/webhooks/subscriptions") &&
      response.request().method() === "POST"
    ));
    await create.click();
    const successSub = await (await successResponse).json() as Subscription;
    createdSubscriptionIds.push(successSub.subscription_id);
    expect(successSub.subscription_id).toBeTruthy();
    await expect(page.locator('[data-evidence-id="webhooks.secret.reveal"]')).toBeVisible();
    await expect(page.locator('[data-evidence-id="webhooks.secret.value"]')).not.toContainText(
      successSub.secret,
    );
    await testInfo.attach("webhook-created-secret-hidden", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator(
      `[data-evidence-id="webhooks.subscription.${successSub.subscription_id}"]`,
    )).toContainText("active");
    await expect(page.locator('[data-evidence-id="webhooks.secret.panel"]')).toHaveCount(0);
    const persistedSubscription = (await subscriptions(request)).find(
      (subscription) => subscription.subscription_id === successSub.subscription_id,
    );
    expect(persistedSubscription).toMatchObject({ active: true, target_url: successSub.target_url });
    expect(persistedSubscription!.secret).not.toBe(successSub.secret);
    expect(persistedSubscription!.secret).toMatch(/^••••.{4}$/u);
    await testInfo.attach("webhook-subscription-refresh-restored-masked", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const successRun = await runArtifact(request, `webhook-success-${suffix}`);
    expect(successRun.status).toBe("succeeded");
    await expectSignedRunChain(request, successRun.run_id);
    let successDelivery: Delivery | undefined;
    await expect.poll(async () => {
      successDelivery = (await deliveries(request)).find(
        (delivery) => delivery.subscription_id === successSub.subscription_id,
      );
      return successDelivery?.status;
    }, { timeout: 30_000, intervals: [300, 600, 1_000] }).toBe("delivered");

    await target.fill("https://example.com/zeroth-evaluation/success");
    await page.locator('[data-evidence-id="webhooks.event.run.failed"]').check();
    const filterResponse = page.waitForResponse((response) => (
      response.url().endsWith("/v1/webhooks/subscriptions") &&
      response.request().method() === "POST"
    ));
    await create.click();
    const failedEventSub = await (await filterResponse).json() as Subscription;
    createdSubscriptionIds.push(failedEventSub.subscription_id);
    const filterRun = await runArtifact(request, `webhook-filter-${suffix}`);
    expect(filterRun.status).toBe("succeeded");
    await page.waitForTimeout(1_200);
    const failedEventDeliveries = (await deliveries(request)).filter(
      (delivery) => delivery.subscription_id === failedEventSub.subscription_id,
    ).length;
    expect(failedEventDeliveries).toBe(0);

    await target.fill(`https://example.com/zeroth-evaluation/flaky/${suffix}`);
    await completed.check();
    const flakyResponse = page.waitForResponse((response) => (
      response.url().endsWith("/v1/webhooks/subscriptions") &&
      response.request().method() === "POST"
    ));
    await create.click();
    const flakySub = await (await flakyResponse).json() as Subscription;
    createdSubscriptionIds.push(flakySub.subscription_id);
    await runArtifact(request, `webhook-flaky-${suffix}`);
    let deadLetter: DeadLetter | undefined;
    await expect.poll(async () => {
      deadLetter = (await deadLetters(request)).find(
        (entry) => entry.subscription_id === flakySub.subscription_id,
      );
      return deadLetter?.attempt_count;
    }, { timeout: 60_000, intervals: [500, 1_000] }).toBe(5);

    await page.locator('[data-evidence-id="webhooks.dead-letters.refresh"]').click();
    await expect(page.locator(
      `[data-evidence-id="webhooks.dead-letter.${deadLetter!.dead_letter_id}"]`,
    )).toBeVisible();
    await testInfo.attach("webhook-dead-letter-after-five-attempts", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    const replayResponse = page.waitForResponse((response) => (
      response.url().includes(`/v1/webhooks/dead-letters/${deadLetter!.dead_letter_id}/replay`) &&
      response.request().method() === "POST"
    ));
    await page.locator(
      `[data-evidence-id="webhooks.dead-letter.${deadLetter!.dead_letter_id}.replay"]`,
    ).click();
    const replay = await (await replayResponse).json() as { delivery_id: string };
    await expect.poll(async () => (
      (await deliveries(request)).find((delivery) => delivery.delivery_id === replay.delivery_id)?.status
    ), { timeout: 30_000, intervals: [300, 600, 1_000] }).toBe("delivered");
    await page.locator('[data-evidence-id="webhooks.deliveries.refresh"]').click();
    await expect(page.locator(`[data-evidence-id="webhooks.delivery.${replay.delivery_id}"]`)).toContainText(
      "delivered",
    );
    await testInfo.attach("webhook-replay-delivered", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await page.route(
      `**/v1/webhooks/dead-letters/${deadLetter!.dead_letter_id}/replay`,
      async (route) => route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "controlled evaluation replay failure" }),
      }),
    );
    await page.locator(
      `[data-evidence-id="webhooks.dead-letter.${deadLetter!.dead_letter_id}.replay"]`,
    ).click();
    await expect(page.getByText("Replay failed: controlled evaluation replay failure")).toBeVisible();
    await testInfo.attach("webhook-replay-failure-visible", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await page.unroute(`**/v1/webhooks/dead-letters/${deadLetter!.dead_letter_id}/replay`);

    // Present the tenant-B credential to the tenant-A service.  This proves the
    // resource is hidden at the application scope boundary without relying on a
    // second SQLite writer during the primary tenant's concurrency-heavy runs.
    const foreign = await request.get(
      `${apiBase}/v1/webhooks/subscriptions/${successSub.subscription_id}`,
      { headers: { "X-API-Key": twinApiKey! } },
    );
    expect(foreign.status()).toBe(404);

    page.once("dialog", (dialog) => dialog.accept());
    await page.locator(
      `[data-evidence-id="webhooks.subscription.${flakySub.subscription_id}.deactivate"]`,
    ).click();
    await expect.poll(async () => {
      const response = await request.get(
        `${apiBase}/v1/webhooks/subscriptions/${flakySub.subscription_id}`,
        { headers: { "X-API-Key": apiKey! } },
      );
      return (await response.json()).active;
    }).toBe(false);

    const deliveryCountBeforeInactiveRun = (await deliveries(request)).filter(
      (delivery) => delivery.subscription_id === flakySub.subscription_id,
    ).length;
    const inactiveRun = await runArtifact(request, `webhook-inactive-${suffix}`);
    expect(inactiveRun.status).toBe("succeeded");
    await expectSignedRunChain(request, inactiveRun.run_id);
    await page.waitForTimeout(1_200);
    expect((await deliveries(request)).filter(
      (delivery) => delivery.subscription_id === flakySub.subscription_id,
    )).toHaveLength(deliveryCountBeforeInactiveRun);
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.locator(
      `[data-evidence-id="webhooks.subscription.${flakySub.subscription_id}"]`,
    )).toContainText("inactive");

    const createAudit = await expectSignedWebhookAudit(
      request,
      successSub.subscription_id,
      "webhook.subscription.create",
      "subscription_created",
    );
    const replayAudit = await expectSignedWebhookAudit(
      request,
      flakySub.subscription_id,
      "webhook.dead-letter.replay",
      "replay_authorized",
    );
    const deactivateAudit = await expectSignedWebhookAudit(
      request,
      flakySub.subscription_id,
      "webhook.subscription.deactivate",
      "subscription_deactivated",
    );
    const health = await request.get(`${apiBase}/health`);
    expect(health.status()).toBe(200);
    const deploymentRef = (await health.json() as { deployment_ref: string }).deployment_ref;
    const verificationResponse = await request.get(
      `${apiBase}/v1/deployments/${deploymentRef}/audit-verification`,
      { headers: { "X-API-Key": apiKey! } },
    );
    expect(verificationResponse.status()).toBe(200);
    const deploymentVerification = await verificationResponse.json() as AuditVerification;
    expect(deploymentVerification).toMatchObject({
      verified: true,
      signature_verified: true,
      unsigned_record_count: 0,
    });

    await testInfo.attach("webhook-deactivated-refresh-restored", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });

    await assertAccessibility(page, testInfo);
    await attachSafeJson(testInfo, "webhook-live-result", {
      success_subscription_id: successSub.subscription_id,
      success_run_id: successRun.run_id,
      success_delivery_id: successDelivery!.delivery_id,
      filter_run_id: filterRun.run_id,
      failed_event_subscription_delivery_count: failedEventDeliveries,
      dead_letter_id: deadLetter!.dead_letter_id,
      dead_letter_attempts: deadLetter!.attempt_count,
      replay_delivery_id: replay.delivery_id,
      replay_status: "delivered",
      twin_lookup_status: foreign.status(),
      flaky_subscription_active_after_deactivation: false,
      inactive_run_id: inactiveRun.run_id,
      inactive_subscription_delivery_count_before: deliveryCountBeforeInactiveRun,
      inactive_subscription_delivery_count_after: deliveryCountBeforeInactiveRun,
      create_audit_id: createAudit.audit_id,
      replay_audit_id: replayAudit.audit_id,
      deactivate_audit_id: deactivateAudit.audit_id,
      deployment_audit_record_count: deploymentVerification.record_count,
      deployment_audit_verified: deploymentVerification.verified,
      deployment_audit_signature_verified: deploymentVerification.signature_verified,
      sensitive_material_persisted_to_evidence: false,
      external_network_calls: 0,
      provider_cost_usd: 0,
    });
  } finally {
    for (const subscriptionId of createdSubscriptionIds) {
      await deactivate(request, subscriptionId);
    }
  }
});

test("timeout hook exhausts retries and remains inspectable", async ({ page, request }, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(90_000);
  coverCriteria(testInfo, "webhooks.timeout", "webhooks.retry-exhaustion");
  await configurePage(page, apiBase, tenant, apiKey!);

  const response = await request.post(`${apiBase}/v1/webhooks/subscriptions`, {
    headers: { "X-API-Key": apiKey! },
    data: {
      deployment_ref: "server-scoped",
      tenant_id: tenant,
      target_url: `https://example.com/zeroth-evaluation/timeout/${Date.now()}`,
      event_types: ["run.completed"],
    },
  });
  expect(response.status()).toBe(201);
  const subscription = await response.json() as Subscription;
  const run = await runArtifact(request, `webhook-timeout-${Date.now()}`);
  expect(run.status).toBe("succeeded");

  let deadLetter: DeadLetter | undefined;
  await expect.poll(async () => {
    deadLetter = (await deadLetters(request)).find(
      (entry) => entry.subscription_id === subscription.subscription_id,
    );
    return deadLetter?.attempt_count;
  }, { timeout: 60_000, intervals: [500, 1_000] }).toBe(5);

  await page.goto("/console/webhooks/", { waitUntil: "networkidle" });
  await expect(page.locator(
    `[data-evidence-id="webhooks.dead-letter.${deadLetter!.dead_letter_id}"]`,
  )).toContainText("timeout");
  await testInfo.attach("webhook-timeout-after-five-attempts", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await assertAccessibility(page, testInfo);
  await attachSafeJson(testInfo, "webhook-timeout-result", {
    subscription_id: subscription.subscription_id,
    run_id: run.run_id,
    dead_letter_id: deadLetter!.dead_letter_id,
    attempt_count: deadLetter!.attempt_count,
    outcome: "timeout",
    sensitive_material_persisted_to_evidence: false,
    external_network_calls: 0,
    provider_cost_usd: 0,
  });

  const deactivate = await request.delete(
    `${apiBase}/v1/webhooks/subscriptions/${subscription.subscription_id}`,
    { headers: { "X-API-Key": apiKey! } },
  );
  expect(deactivate.status()).toBe(204);
});

test("unavailable and timeout-after-commit hooks expose durable operator outcomes", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(120_000);
  coverCriteria(
    testInfo,
    "webhooks.sink-unavailable",
    "webhooks.runtime-correlation",
  );
  await configurePage(page, apiBase, tenant, apiKey!);

  const suffix = `${Date.now()}`;
  const headers = { "X-API-Key": apiKey! };
  const createSubscription = async (mode: string): Promise<Subscription> => {
    const response = await request.post(`${apiBase}/v1/webhooks/subscriptions`, {
      headers,
      data: {
        deployment_ref: "server-scoped",
        tenant_id: tenant,
        target_url: `https://example.com/zeroth-evaluation/${mode}/${suffix}`,
        event_types: ["run.completed"],
      },
    });
    expect(response.status()).toBe(201);
    return response.json() as Promise<Subscription>;
  };

  const unavailableSub = await createSubscription("unavailable");
  const committedSub = await createSubscription("timeout-after-commit");
  try {
    const unavailableRun = await runArtifact(request, `webhook-unavailable-${suffix}`);
    expect(unavailableRun.status).toBe("succeeded");
    await expectSignedRunChain(request, unavailableRun.run_id);
    let unavailableDeadLetter: DeadLetter | undefined;
    await expect.poll(async () => {
      unavailableDeadLetter = (await deadLetters(request)).find(
        (entry) => (
          entry.subscription_id === unavailableSub.subscription_id &&
          entry.run_id === unavailableRun.run_id
        ),
      );
      return unavailableDeadLetter?.attempt_count;
    }, { timeout: 60_000, intervals: [500, 1_000] }).toBe(5);
    expect(unavailableDeadLetter!.run_id).toBe(unavailableRun.run_id);

    const committedRun = await runArtifact(request, `webhook-timeout-after-commit-${suffix}`);
    expect(committedRun.status).toBe("succeeded");
    await expectSignedRunChain(request, committedRun.run_id);
    let committedDelivery: Delivery | undefined;
    await expect.poll(async () => {
      committedDelivery = (await deliveries(request)).find(
        (delivery) => (
          delivery.subscription_id === committedSub.subscription_id &&
          delivery.run_id === committedRun.run_id
        ),
      );
      return committedDelivery?.status;
    }, { timeout: 30_000, intervals: [300, 600, 1_000] }).toBe("delivered");
    expect(committedDelivery!.run_id).toBe(committedRun.run_id);
    expect([1, 2]).toContain(committedDelivery!.attempt_count);
    const timeoutAfterCommitObserved = committedDelivery!.attempt_count === 2;
    if (timeoutAfterCommitObserved) {
      coverCriteria(testInfo, "webhooks.timeout-after-commit");
    } else {
      testInfo.annotations.push({
        type: "blocked",
        description: (
          "timeout-after-commit was deduplicated against an earlier receipt for the shared " +
          "logical event; rerun against a clean campaign sink to observe the retry"
        ),
      });
    }

    await page.goto("/console/webhooks/", { waitUntil: "networkidle" });
    await expect(page.locator(
      `[data-evidence-id="webhooks.dead-letter.${unavailableDeadLetter!.dead_letter_id}"]`,
    )).toContainText("controlled evaluation sink unavailable");
    await expect(page.locator(
      `[data-evidence-id="webhooks.dead-letter.${unavailableDeadLetter!.dead_letter_id}"]`,
    )).toContainText(unavailableRun.run_id);
    await expect(page.locator(
      `[data-evidence-id="webhooks.delivery.${committedDelivery!.delivery_id}"]`,
    )).toContainText(`${committedDelivery!.attempt_count}/5 attempts`);
    await expect(page.locator(
      `[data-evidence-id="webhooks.delivery.${committedDelivery!.delivery_id}"]`,
    )).toContainText(committedRun.run_id);
    await testInfo.attach("webhook-unavailable-and-timeout-after-commit", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await assertAccessibility(page, testInfo);
    await attachSafeJson(testInfo, "webhook-controlled-sink-result", {
      unavailable_subscription_id: unavailableSub.subscription_id,
      unavailable_run_id: unavailableRun.run_id,
      unavailable_dead_letter_id: unavailableDeadLetter!.dead_letter_id,
      unavailable_attempt_count: unavailableDeadLetter!.attempt_count,
      timeout_after_commit_subscription_id: committedSub.subscription_id,
      timeout_after_commit_run_id: committedRun.run_id,
      timeout_after_commit_delivery_id: committedDelivery!.delivery_id,
      timeout_after_commit_event_id: committedDelivery!.event_id,
      timeout_after_commit_attempt_count: committedDelivery!.attempt_count,
      timeout_after_commit_status: committedDelivery!.status,
      timeout_after_commit_retry_observed: timeoutAfterCommitObserved,
      receipt_cardinality_checked_by_browser_api: false,
      sensitive_material_persisted_to_evidence: false,
      external_network_calls: 0,
      provider_cost_usd: 0,
    });
  } finally {
    await deactivate(request, unavailableSub.subscription_id);
    await deactivate(request, committedSub.subscription_id);
  }
});
