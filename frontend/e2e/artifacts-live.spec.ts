import { expect, test, type APIRequestContext } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

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

type ArtifactRef = { key: string; content_type: string; size: number };
type Run = {
  run_id: string;
  status: string;
  tenant_id: string;
  deployment_ref: string;
  graph_version_ref: string;
  thread_id: string;
  terminal_output?: { artifact?: ArtifactRef };
};

type RunEvidence = {
  run: Run;
  audits: Array<{
    audit_id: string;
    run_id: string;
    deployment_ref: string;
    graph_version_ref: string;
    node_id: string;
    cost_usd: number | null;
    estimated_cost_usd: number | null;
    cost_event_id: string | null;
    erased: boolean;
  }>;
  summary: {
    audit_count: number;
    priced_call_count: number;
    cost_event_count: number;
    total_cost_usd: number;
    reconciliation_state: string;
  };
};

type AuditVerification = {
  verified: boolean;
  signature_verified: boolean | null;
  unsigned_record_count: number;
  record_count: number;
};

async function verifiedRunEvidence(
  request: APIRequestContext,
  run: Run,
): Promise<{ verification: AuditVerification; evidence: RunEvidence }> {
  const verificationResponse = await request.post(`${apiBase}/v1/runs/${run.run_id}/verify-chain`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(verificationResponse.status()).toBe(200);
  const verification = await verificationResponse.json() as AuditVerification;
  expect(verification.verified).toBe(true);
  expect(verification.signature_verified).toBe(true);
  expect(verification.unsigned_record_count).toBe(0);
  expect(verification.record_count).toBeGreaterThan(0);

  const evidenceResponse = await request.get(`${apiBase}/v1/runs/${run.run_id}/evidence`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(evidenceResponse.status()).toBe(200);
  const evidence = await evidenceResponse.json() as RunEvidence;
  expect(evidence.run.run_id).toBe(run.run_id);
  expect(evidence.run.tenant_id).toBe(tenant);
  expect(evidence.run.deployment_ref).toBe(run.deployment_ref);
  expect(evidence.run.graph_version_ref).toBe(run.graph_version_ref);
  expect(evidence.audits.length).toBeGreaterThan(0);
  expect(evidence.audits.every((audit) =>
    audit.run_id === run.run_id
      && audit.deployment_ref === run.deployment_ref
      && audit.graph_version_ref === run.graph_version_ref
      && audit.node_id.length > 0
  )).toBe(true);
  expect(evidence.summary.audit_count).toBe(evidence.audits.length);
  expect(evidence.summary.priced_call_count).toBe(0);
  expect(evidence.summary.cost_event_count).toBe(0);
  expect(evidence.summary.total_cost_usd).toBe(0);
  expect(evidence.audits.every((audit) =>
    (audit.cost_usd ?? 0) === 0
      && (audit.estimated_cost_usd ?? 0) === 0
      && audit.cost_event_id == null
  )).toBe(true);
  return { verification, evidence };
}

async function createArtifact(
  request: APIRequestContext,
  kind: "json" | "text" | "image" | "binary",
  options: { ttl_seconds?: number; size_bytes?: number; label?: string } = {},
): Promise<{ run: Run; artifact: ArtifactRef }> {
  const response = await request.post(`${apiBase}/v1/runs`, {
    headers: { "X-API-Key": apiKey! },
    data: {
      input_payload: {
        kind,
        label: options.label ?? `${kind}-playwright-fixture`,
        ...(options.ttl_seconds == null ? {} : { ttl_seconds: options.ttl_seconds }),
        ...(options.size_bytes == null ? {} : { size_bytes: options.size_bytes }),
      },
      campaign_id: campaignId,
      campaign_strict: true,
    },
  });
  expect(response.status()).toBe(202);
  const created = await response.json() as Run;
  let run = created;
  await expect.poll(async () => {
    const current = await request.get(`${apiBase}/v1/runs/${created.run_id}`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(current.status()).toBe(200);
    run = await current.json() as Run;
    return run.status;
  }, { timeout: 20_000, intervals: [200, 400, 800] }).toBe("succeeded");
  expect(run.terminal_output?.artifact).toBeTruthy();
  expect(run.tenant_id).toBe(tenant);
  expect(run.deployment_ref).toBe(
    process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF ?? "demo-artifact-output-v1",
  );
  expect(run.graph_version_ref).toBe(
    process.env.ZEROTH_EVALUATION_GRAPH_VERSION ?? "evaluation-studio-v1-artifact-output@2",
  );
  return { run, artifact: run.terminal_output!.artifact! };
}

test("workflow artifacts preview, download, isolate, expire, and erase through the console", async ({
  page,
  request,
}, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  expect(twinApiKey, "ZEROTH_EVALUATION_TWIN_API_KEY is required").toBeTruthy();
  // Four real runtime executions plus four signed-chain/evidence reconciliations
  // intentionally trade speed for durable acceptance evidence. Keep the UI
  // assertions on their normal short timeouts while allowing the fixture setup
  // to complete on a busy local SQLite campaign service.
  test.setTimeout(180_000);
  coverCriteria(
    testInfo,
    "artifacts.workflow-output",
    "artifacts.preview-download",
    "artifacts.tenant-isolation",
    "artifacts.expiry",
    "artifacts.erasure",
  );
  await configurePage(page, apiBase, tenant, apiKey!);

  const json = await createArtifact(request, "json");
  const text = await createArtifact(request, "text");
  const image = await createArtifact(request, "image");
  const binary = await createArtifact(request, "binary", { size_bytes: 1_200_000 });
  const correlated = await Promise.all(
    [json.run, text.run, image.run, binary.run].map((run) => verifiedRunEvidence(request, run)),
  );

  await page.goto("/console/artifacts/", { waitUntil: "networkidle" });
  await expect(page.getByText(json.artifact.key, { exact: true })).toBeVisible();
  await testInfo.attach("artifacts-discovered-from-live-runs", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.locator('[data-evidence-id="artifacts-load"]').click();
  await expect(page.getByText("Enter an artifact ID before loading it.")).toBeVisible();
  await testInfo.attach("artifact-required-empty-validation", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.getByText(json.artifact.key, { exact: true }).click();
  await expect(page.getByText('"source": "zeroth-live-evaluation"')).toBeVisible();
  await testInfo.attach("artifact-json-preview", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.getByText(text.artifact.key, { exact: true }).click();
  await expect(page.getByText(/Zeroth artifact fixture: text-playwright-fixture/)).toBeVisible();

  await page.getByText(image.artifact.key, { exact: true }).click();
  await expect(page.locator('[data-evidence-id="artifacts-image-preview"]')).toBeVisible();
  await testInfo.attach("artifact-image-preview", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await page.getByText(binary.artifact.key, { exact: true }).click();
  await expect(page.getByText(/Binary content is available for download/)).toBeVisible({
    timeout: 20_000,
  });
  const downloadPromise = page.waitForEvent("download");
  await page.locator('[data-evidence-id="artifacts-download"]').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe(binary.artifact.key.split("/").at(-1));
  const downloadedPath = await download.path();
  expect(downloadedPath).toBeTruthy();
  const downloaded = await readFile(downloadedPath!);
  const binaryResponse = await request.get(`${apiBase}/v1/artifacts/${binary.artifact.key}`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(binaryResponse.status()).toBe(200);
  const stored = await binaryResponse.body();
  const downloadedHash = createHash("sha256").update(downloaded).digest("hex");
  const storedHash = createHash("sha256").update(stored).digest("hex");
  expect(downloaded.length).toBe(binary.artifact.size);
  expect(downloadedHash).toBe(storedHash);
  await testInfo.attach("artifact-large-binary-download", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  // Use the foreign tenant credential against the same healthy service. The
  // opaque 404 proves both authentication scope and artifact-store namespace
  // isolation without making acceptance depend on another deployment's uptime.
  const foreign = await request.get(`${apiBase}/v1/artifacts/${json.artifact.key}`, {
    headers: { "X-API-Key": twinApiKey! },
  });
  expect(foreign.status()).toBe(404);

  const expiring = await createArtifact(request, "text", {
    ttl_seconds: 1,
    label: "expiring-playwright-fixture",
  });
  await page.waitForTimeout(1_300);
  const expired = await request.get(`${apiBase}/v1/artifacts/${expiring.artifact.key}`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(expired.status()).toBe(404);

  const erasable = await createArtifact(request, "json", { label: "erasable-playwright-fixture" });
  const erased = await request.post(`${apiBase}/v1/retention/erasure-requests`, {
    headers: { "X-API-Key": apiKey! },
    data: { run_id: erasable.run.run_id },
  });
  expect(erased.status()).toBe(200);
  const erasedResult = await erased.json() as { runs: { artifacts_deleted: number }[] };
  expect(erasedResult.runs[0].artifacts_deleted).toBe(1);
  const erasedLookup = await request.get(`${apiBase}/v1/artifacts/${erasable.artifact.key}`, {
    headers: { "X-API-Key": apiKey! },
  });
  expect(erasedLookup.status()).toBe(404);
  const erasedVerification = await request.post(
    `${apiBase}/v1/runs/${erasable.run.run_id}/verify-chain`,
    { headers: { "X-API-Key": apiKey! } },
  );
  expect(erasedVerification.status()).toBe(200);
  const erasedChain = await erasedVerification.json() as AuditVerification;
  expect(erasedChain.verified).toBe(true);
  expect(erasedChain.signature_verified).toBe(true);
  expect(erasedChain.unsigned_record_count).toBe(0);

  const input = page.locator('[data-evidence-id="artifacts-id-input"]');
  await input.fill(erasable.artifact.key);
  await page.locator('[data-evidence-id="artifacts-load"]').click();
  await expect(page.getByText(/404|not found/i).last()).toBeVisible();
  await testInfo.attach("artifact-erased-ui-state", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await assertAccessibility(page, testInfo);
  await attachSafeJson(testInfo, "artifact-lifecycle-result", {
    deployment_ref: process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF ?? "demo-artifact-output-v1",
    graph_version_ref: process.env.ZEROTH_EVALUATION_GRAPH_VERSION ?? "evaluation-studio-v1-artifact-output@2",
    runs: [json.run.run_id, text.run.run_id, image.run.run_id, binary.run.run_id],
    artifacts: [json.artifact, text.artifact, image.artifact, binary.artifact],
    twin_tenant_lookup_status: foreign.status(),
    expired_lookup_status: expired.status(),
    erased_lookup_status: erasedLookup.status(),
    erased_artifact_count: erasedResult.runs[0].artifacts_deleted,
    erased_chain: erasedChain,
    audit_correlations: correlated.map(({ verification, evidence }) => ({
      run_id: evidence.run.run_id,
      thread_id: evidence.run.thread_id,
      deployment_ref: evidence.run.deployment_ref,
      graph_version_ref: evidence.run.graph_version_ref,
      audit_ids: evidence.audits.map((audit) => audit.audit_id),
      node_ids: [...new Set(evidence.audits.map((audit) => audit.node_id))],
      audit_verification: verification,
      summary: evidence.summary,
    })),
    binary_download: {
      size_bytes: downloaded.length,
      sha256: downloadedHash,
      matches_stored_artifact: downloadedHash === storedHash,
    },
    provider_cost_usd: 0,
  });
});
