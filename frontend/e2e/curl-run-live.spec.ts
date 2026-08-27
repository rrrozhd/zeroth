import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { promisify } from "node:util";

import { expect, test } from "@playwright/test";

import { attachSafeJson, configurePage, coverCriteria } from "./support/live-evaluation";

const execFileAsync = promisify(execFile);
const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8122";
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const campaignId = process.env.ZEROTH_EVALUATION_CAMPAIGN_ID ?? "evaluation-studio-v1";
const workflowId = process.env.ZEROTH_EVALUATION_WORKFLOW_ID
  ?? "43dc0a14-e924-4d3e-8763-740408ebee3a";
const workflowVersion = Number(process.env.ZEROTH_EVALUATION_WORKFLOW_VERSION ?? "2");
const deploymentRef = process.env.ZEROTH_EVALUATION_DEPLOYMENT_REF
  ?? "demo-data-quality-repair-loop-manifest-v1";
const payload = '{"records":[{"name":" Grace ","email":"GRACE@EXAMPLE.TEST ","status":"unknown"}]}';

test("the exact safe cURL copied from Studio creates a reviewable run", async ({ page, request }, testInfo) => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");
  test.skip(testInfo.project.name !== "desktop-1440", "functional checkpoint is captured once");
  expect(apiKey, "ZEROTH_EVALUATION_API_KEY is required").toBeTruthy();
  test.setTimeout(60_000);
  coverCriteria(testInfo, "runs.curl-copy", "runs.curl-execution", "runs.detail", "manifests.execution");

  await configurePage(page, apiBase, tenant, apiKey!);
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          (window as Window & { __zerothCopiedCommand?: string }).__zerothCopiedCommand = text;
        },
      },
    });
  });

  const health = await (await request.get(`${apiBase}/health`)).json() as {
    deployment_ref: string;
    graph_version_ref: string;
  };
  expect(health).toMatchObject({ deployment_ref: deploymentRef });
  expect(health.graph_version_ref).toBe(`${workflowId}@${workflowVersion}`);

  await page.goto(`/console/studio/edit/?id=${workflowId}`, { waitUntil: "domcontentloaded" });
  const dock = page.locator(".studio-run-dock");
  await expect(dock).toBeVisible();
  await dock.getByRole("button", { name: "Run", exact: true }).click();
  await dock.getByRole("textbox", { name: /Input payload/ }).fill(payload);
  await dock.getByText("Call this API with cURL", { exact: true }).click();
  await expect(dock.getByText("Uses the shell's ZEROTH_API_KEY; no credential is copied.", { exact: true })).toBeVisible();
  await testInfo.attach("curl-configured-safe-command", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });

  await dock.getByRole("button", { name: "Copy", exact: true }).click();
  await expect(dock.getByRole("button", { name: "Copied ✓", exact: true })).toBeVisible();
  const command = await page.evaluate(() =>
    (window as Window & { __zerothCopiedCommand?: string }).__zerothCopiedCommand,
  );
  expect(command).toBeTruthy();
  expect(command).toContain(`curl -fsS -X POST "${apiBase}/v1/runs"`);
  expect(command).toContain(`"campaign_id": "${campaignId}"`);
  expect(command).toContain("X-API-Key: $ZEROTH_API_KEY");
  expect(command).not.toContain(apiKey!);
  expect(command).not.toContain("<run_id>");
  expect(command).not.toMatch(/sk-(?:proj-)?[A-Za-z0-9_-]{20,}/);

  const { stdout, stderr } = await execFileAsync("/bin/zsh", ["-lc", command!], {
    env: { ...process.env, ZEROTH_API_KEY: apiKey! },
    timeout: 20_000,
  });
  expect(stderr).toBe("");
  const submitted = JSON.parse(stdout) as { run_id: string; status: string };
  expect(submitted.run_id).toMatch(/^[a-f0-9]{32}$/);

  let terminal: Record<string, unknown> | null = null;
  await expect.poll(async () => {
    const response = await request.get(`${apiBase}/v1/runs/${submitted.run_id}`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    terminal = await response.json() as Record<string, unknown>;
    return terminal.status;
  }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe("succeeded");
  expect(terminal!.deployment_ref).toBe(deploymentRef);

  await page.goto(`/console/runs/?run=${submitted.run_id}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByText(submitted.run_id, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(deploymentRef, { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", {
    name: new RegExp(`^${submitted.run_id} succeeded ${deploymentRef}`),
  })).toBeVisible();
  await testInfo.attach("curl-created-run-in-runs-ui", {
    body: await page.screenshot({ fullPage: true, animations: "disabled" }),
    contentType: "image/png",
  });
  await attachSafeJson(testInfo, "curl-run-result", {
    command_method: "POST",
    command_url: `${apiBase}/v1/runs`,
    command_sha256: createHash("sha256").update(command!).digest("hex"),
    environment_key_reference_used: true,
    run_id: submitted.run_id,
    status: terminal!.status,
    deployment_ref: terminal!.deployment_ref,
    graph_version_ref: terminal!.graph_version_ref,
    terminal_output: terminal!.terminal_output,
  });
});
