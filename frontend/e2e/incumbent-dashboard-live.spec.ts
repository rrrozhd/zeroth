import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { existsSync } from "node:fs";

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
const operatorApiKey = process.env.ZEROTH_EVALUATION_OPERATOR_API_KEY;
const incidentLoopWorkflowId =
  process.env.ZEROTH_EVALUATION_INCIDENT_LOOP_WORKFLOW_ID ??
  "da5da69b-1086-4cfe-8090-424a0118b88c";
const approvalWorkflowId =
  process.env.ZEROTH_EVALUATION_APPROVAL_WORKFLOW_ID ??
  "evaluation-studio-v1-governed-remediation";

test.describe("incumbent dashboard live acceptance", () => {
  test.skip(!liveEnabled, "requires the isolated local evaluation service");

  test.beforeEach(async ({ page }, testInfo) => {
    const configuredKey = testInfo.title.includes("operator ")
      ? operatorApiKey
      : apiKey;
    expect(configuredKey, "the role-scoped Zeroth API key is required").toBeTruthy();
    await configurePage(page, apiBase, tenant, configuredKey!);
  });

  test("all incumbent dashboards expose meaningful tenant data", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one evidence pass is sufficient");
    test.setTimeout(60_000);

    const runsResponse = await request.get(`${apiBase}/v1/admin/runs`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(runsResponse.status()).toBe(200);
    const runBody = await runsResponse.json() as { total: number };
    expect(runBody.total).toBeGreaterThanOrEqual(9);

    const checks = [
      { path: "/runs", visible: ["demo-data-quality-repair-loop", "demo-incident-readiness-loop"] },
      { path: "/audit", visible: ["Audit records", "Workflow", "Security"] },
      { path: "/cost", visible: ["Actual provider spend", "Run-attributed economics"] },
      { path: "/retention", visible: ["[SYNTHETIC DEMO] Preserve", "run 379e3364"] },
      { path: "/rightsizing", visible: ["Suggest a cheaper model", "Run experiment"] },
      { path: "/regulus/capabilities", visible: ["Demo agent", "research"] },
      { path: "/regulus/enforcement", visible: ["synthetic demo", "TriggerInvestigation", "approved", "rejected"] },
      { path: "/regulus/reconciliation", visible: ["Calibration summary"] },
      { path: "/metrics", visible: ["Metrics"] },
      { path: "/studio", visible: ["Customer data quality repair", "Incident readiness review"] },
    ] as const;

    for (const check of checks) {
      await page.goto(`/console${check.path}/`, { waitUntil: "networkidle" });
      await expect(page.locator("main")).toBeVisible();
      for (const text of check.visible) {
        await expect(page.getByText(text, { exact: false }).first()).toBeVisible();
      }
      await page.screenshot({
        path: testInfo.outputPath(`${check.path.replaceAll("/", "-").replace(/^-/, "") || "overview"}.png`),
        fullPage: true,
        animations: "disabled",
      });
    }

    await attachSafeJson(testInfo, "dashboard-acceptance", {
      tenant,
      run_count: runBody.total,
      routes: checks.map((check) => check.path),
      provider_calls_made: 0,
      fixture_provenance: "synthetic-demo labels are rendered on seeded governance records",
    });
  });

  test("run evidence is reviewable without expanding redacted payload JSON", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one evidence pass is sufficient");
    await page.goto("/console/runs/?run=7f2b9a5c16534323995975b1a34deec5", { waitUntil: "networkidle" });

    await expect(page.getByText("Payload values are intentionally withheld", { exact: false })).toBeVisible();
    await expect(page.getByRole("button", { name: "Show raw evidence" })).toBeVisible();
    await expect(page.getByText("digest", { exact: false }).first()).toBeVisible();
    await expect(page.getByText("***REDACTED***", { exact: true })).toHaveCount(0);

    const axe = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    const violations = axe.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target),
    }));
    await attachSafeJson(testInfo, "run-evidence-axe", violations);
    expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);
  });

  test("operator deterministic rightsizing lookup returns capability-matched candidates without a provider call", async ({ page }, testInfo) => {
    coverCriteria(
      testInfo,
      "economics-and-rightsizing.rightsizing-static",
      "rightsizing.static-recommendation",
    );
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    const consoleErrors: string[] = [];
    let measuredRequests = 0;
    let staticRequests = 0;
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname.endsWith("/v1/econ/rightsizing")
      ) staticRequests += 1;
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname.endsWith("/v1/econ/rightsizing/experiment")
      ) measuredRequests += 1;
    });
    await page.goto("/console/rightsizing/", { waitUntil: "networkidle" });

    // Static recommendations require WORKFLOW_READ, while the passive opportunity
    // card requires METRICS_READ. Keep those authorization boundaries independent:
    // an operator can prove the provider-free lookup even when tenant metrics are
    // intentionally unavailable to that role.
    const opportunitiesDenied = page.locator(
      '[data-evidence-id="rightsizing.opportunities.error"]',
    );
    const opportunitiesEmpty = page.getByText(
      "No replayable workflow-agent spend",
      { exact: false },
    );
    await expect(opportunitiesDenied.or(opportunitiesEmpty)).toBeVisible();
    const opportunitiesState = await opportunitiesDenied.isVisible()
      ? "metrics_read_denied"
      : "no_replayable_workflow_agent_spend";
    if (opportunitiesState === "metrics_read_denied") {
      await expect(opportunitiesDenied).toContainText("Metrics read permission is required");
    } else {
      await expect(page.getByText("control.corpus-seed.embedding", { exact: false })).toHaveCount(0);
    }

    await page.getByRole("textbox", { name: /incumbent The model you run/ }).fill("gpt-4o");
    await page.getByRole("checkbox", { name: "needs tools" }).first().check();
    await page.getByRole("button", { name: "Find cheaper models" }).click();

    await expect(page.getByText("gpt-5-nano", { exact: false }).first()).toBeVisible();
    await expect(page.getByText("gpt-4o-mini", { exact: false }).first()).toBeVisible();
    expect(staticRequests).toBe(1);
    expect(measuredRequests).toBe(0);
    const candidateRegion = page.locator(
      '[data-evidence-id="rightsizing.region.candidates-scroll"]',
    );
    await expect(candidateRegion).toBeVisible();
    await page.getByRole("button", { name: "Find cheaper models" }).focus();
    await page.keyboard.press("Tab");
    await expect(candidateRegion).toBeFocused();
    expect(await candidateRegion.evaluate((element) => element.matches(":focus-visible"))).toBe(true);
    const refreshed = Promise.all([
      "/v1/econ/rightsizing/opportunities",
      "/v1/econ/unit-economics",
      "/v1/econ/waste",
    ].map((path) => page.waitForResponse((response) => (
      response.request().method() === "GET" && new URL(response.url()).pathname === path
    ))));
    await page.locator('[data-evidence-id="rightsizing.action.refresh"]').click();
    expect((await refreshed).map((response) => response.status())).toEqual([403, 403, 403]);
    await expect(page.getByText("gpt-5-nano", { exact: false }).first()).toBeVisible();
    await expect(page.getByText("gpt-4o-mini", { exact: false }).first()).toBeVisible();
    expect(staticRequests).toBe(1);
    expect(measuredRequests).toBe(0);

    const axe = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    const axeViolations = axe.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target),
    }));
    await attachSafeJson(testInfo, "rightsizing-static-axe-wcag22-aa", axeViolations);
    expect(axeViolations).toEqual([]);
    const unexpectedConsoleErrors = consoleErrors.filter((message) => (
      message !== "Failed to load resource: the server responded with a status of 403 (Forbidden)"
    ));
    expect(consoleErrors).toHaveLength(6);
    expect(unexpectedConsoleErrors, unexpectedConsoleErrors.join("\n")).toEqual([]);
    await attachSafeJson(testInfo, "rightsizing-static-console-summary", {
      total_error_count: consoleErrors.length,
      expected_metrics_denial_resource_errors: consoleErrors.length - unexpectedConsoleErrors.length,
      unexpected_error_count: unexpectedConsoleErrors.length,
    });
    await browserEvidence.attach(testInfo);
    const screenshot = testInfo.outputPath("rightsizing-static-candidates.png");
    await page.screenshot({ path: screenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("rightsizing-static-candidates", {
      path: screenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "rightsizing-static-acceptance", {
      incumbent: "gpt-4o",
      needs_tools: true,
      visible_candidates: ["gpt-5-nano", "gpt-4o-mini"],
      opportunities_state: opportunitiesState,
      provider_calls_made: 0,
      static_requests_sent: staticRequests,
      measured_requests_sent: measuredRequests,
      result_persisted_after_refresh: true,
      observation_basis: "provider-free static route completed; no measured experiment request was sent",
      measured_experiment: "blocked until paid calls share workflow reservation/audit/Regulus instrumentation",
    });
  });

  test("operator deterministic rightsizing fields expose every control, validation, and refresh result", async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    test.skip(
      !["desktop-1440", "webkit-1440"].includes(testInfo.project.name),
      "field contract runs once in Chromium and once in WebKit",
    );
    coverCriteria(
      testInfo,
      "rightsizing.field-contract",
      "economics-and-rightsizing.boundary",
      "ui.keyboard-operation",
    );
    const browserEvidence = new BrowserEvidence(page, new URL(apiBase).origin);
    const consoleErrors: string[] = [];
    let measuredRequests = 0;
    let staticRequests = 0;
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname.endsWith("/v1/econ/rightsizing")
      ) staticRequests += 1;
      if (
        request.method() === "POST" &&
        new URL(request.url()).pathname.endsWith("/v1/econ/rightsizing/experiment")
      ) measuredRequests += 1;
    });

    await page.goto("/console/rightsizing/", { waitUntil: "networkidle" });
    await expect(page.locator('[data-evidence-id="rightsizing.suggest.submit"]')).toBeDisabled();
    await expect(page.locator('[data-evidence-id="rightsizing.experiment.submit"]')).toBeDisabled();

    const missingInitialIds = await page
      .locator("main button:visible, main input:visible, main textarea:visible, main select:visible")
      .evaluateAll((controls) => controls
        .filter((control) => !control.getAttribute("data-evidence-id"))
        .map((control) => control.outerHTML));
    expect(missingInitialIds).toEqual([]);

    const requiredInitialControls = [
      "rightsizing.action.refresh",
      "rightsizing.suggest.incumbent",
      "rightsizing.suggest.min-savings-pct",
      "rightsizing.suggest.limit",
      "rightsizing.suggest.needs-tools",
      "rightsizing.suggest.needs-vision",
      "rightsizing.suggest.submit",
      "rightsizing.experiment.node-id",
      "rightsizing.experiment.incumbent",
      "rightsizing.experiment.instruction",
      "rightsizing.experiment.mode.equivalence",
      "rightsizing.experiment.mode.correctness",
      "rightsizing.experiment.tolerance-pct",
      "rightsizing.experiment.max-cases",
      "rightsizing.experiment.needs-tools",
      "rightsizing.experiment.needs-vision",
      "rightsizing.experiment.judge-model",
      "rightsizing.experiment.max-candidates",
      "rightsizing.experiment.min-cases",
      "rightsizing.experiment.submit",
    ];
    for (const evidenceId of requiredInitialControls) {
      await expect(page.locator(`[data-evidence-id="${evidenceId}"]`)).toBeVisible();
    }

    const equivalence = page.locator(
      '[data-evidence-id="rightsizing.experiment.mode.equivalence"]',
    );
    const correctness = page.locator(
      '[data-evidence-id="rightsizing.experiment.mode.correctness"]',
    );
    await equivalence.focus();
    await expect(equivalence).toBeFocused();
    expect(await equivalence.evaluate((element) => element.matches(":focus-visible"))).toBe(true);
    await page.keyboard.press("ArrowRight");
    await expect(correctness).toHaveAttribute("aria-checked", "true");
    await expect(correctness).toBeFocused();

    const verdictRegion = page.locator(
      '[data-evidence-id="rightsizing.quality-verdict.region"]',
    );
    await expect(verdictRegion).toBeVisible();
    await expect(
      verdictRegion.locator('[data-evidence-id="rightsizing.quality-verdict.verdict"] option'),
    ).toHaveText(["good", "bad", "unknown"]);
    const missingConditionalIds = await verdictRegion
      .locator("button:visible, input:visible, textarea:visible, select:visible")
      .evaluateAll((controls) => controls
        .filter((control) => !control.getAttribute("data-evidence-id"))
        .map((control) => control.outerHTML));
    expect(missingConditionalIds).toEqual([]);

    await page.keyboard.press("ArrowLeft");
    await expect(equivalence).toHaveAttribute("aria-checked", "true");
    await expect(verdictRegion).toHaveCount(0);

    const suggestIncumbent = page.locator(
      '[data-evidence-id="rightsizing.suggest.incumbent"]',
    );
    const minSavings = page.locator(
      '[data-evidence-id="rightsizing.suggest.min-savings-pct"]',
    );
    const limit = page.locator('[data-evidence-id="rightsizing.suggest.limit"]');
    const suggestSubmit = page.locator('[data-evidence-id="rightsizing.suggest.submit"]');
    await suggestIncumbent.fill("gpt-4o");
    await minSavings.fill("-0.1");
    await suggestSubmit.click();
    await expect(page.locator('[data-evidence-id="rightsizing.suggest.error"]')).toContainText(
      "Minimum savings must be a number from 0 through 100.",
    );
    await expect(minSavings).toHaveAttribute("aria-invalid", "true");
    expect(staticRequests).toBe(0);

    await minSavings.fill("100");
    await expect(minSavings).not.toHaveAttribute("aria-invalid", "true");
    await expect(page.locator('[data-evidence-id="rightsizing.suggest.error"]')).toHaveCount(0);
    await limit.fill("21");
    await suggestSubmit.click();
    await expect(page.locator('[data-evidence-id="rightsizing.suggest.error"]')).toContainText(
      "Limit must be a whole number from 1 through 20.",
    );
    await expect(limit).toHaveAttribute("aria-invalid", "true");
    expect(staticRequests).toBe(0);
    await limit.fill("20");
    await expect(limit).not.toHaveAttribute("aria-invalid", "true");
    await expect(page.locator('[data-evidence-id="rightsizing.suggest.error"]')).toHaveCount(0);
    await page.locator('[data-evidence-id="rightsizing.suggest.needs-tools"]').check();
    await page.locator('[data-evidence-id="rightsizing.suggest.needs-vision"]').check();

    await page.locator('[data-evidence-id="rightsizing.experiment.node-id"]').fill("research");
    await page.locator('[data-evidence-id="rightsizing.experiment.incumbent"]').fill("gpt-4o-mini");
    await page.locator('[data-evidence-id="rightsizing.experiment.instruction"]').fill(
      "Answer only from the provided evidence.",
    );
    await page.locator('[data-evidence-id="rightsizing.experiment.judge-model"]').fill(
      "gpt-4o-mini",
    );
    await page.locator('[data-evidence-id="rightsizing.experiment.needs-tools"]').check();
    await page.locator('[data-evidence-id="rightsizing.experiment.needs-vision"]').check();

    const experimentSubmit = page.locator(
      '[data-evidence-id="rightsizing.experiment.submit"]',
    );
    const experimentValidationCases = [
      {
        evidenceId: "rightsizing.experiment.tolerance-pct",
        invalid: "-0.1",
        valid: "100",
        message: "Tolerance must be a number from 0 through 100.",
      },
      {
        evidenceId: "rightsizing.experiment.max-cases",
        invalid: "26",
        valid: "25",
        message: "Maximum cases must be a whole number from 1 through 25.",
      },
      {
        evidenceId: "rightsizing.experiment.max-candidates",
        invalid: "7",
        valid: "6",
        message: "Maximum candidates must be a whole number from 1 through 6.",
      },
      {
        evidenceId: "rightsizing.experiment.min-cases",
        invalid: "51",
        valid: "50",
        message: "Minimum cases must be a whole number from 1 through 50.",
      },
    ];
    const validationResults: Array<{
      evidence_id: string;
      message: string;
      cleared_on_valid_change: boolean;
    }> = [];
    for (const validationCase of experimentValidationCases) {
      const control = page.locator(
        `[data-evidence-id="${validationCase.evidenceId}"]`,
      );
      await control.fill(validationCase.invalid);
      await experimentSubmit.click();
      await expect(page.locator('[data-evidence-id="rightsizing.experiment.error"]')).toContainText(
        validationCase.message,
      );
      await expect(control).toHaveAttribute("aria-invalid", "true");
      expect(measuredRequests).toBe(0);
      await control.fill(validationCase.valid);
      await expect(control).not.toHaveAttribute("aria-invalid", "true");
      await expect(page.locator('[data-evidence-id="rightsizing.experiment.error"]')).toHaveCount(0);
      validationResults.push({
        evidence_id: validationCase.evidenceId,
        message: validationCase.message,
        cleared_on_valid_change: true,
      });
    }

    await correctness.click();
    await expect(correctness).toHaveAttribute("aria-checked", "true");
    await page.locator('[data-evidence-id="rightsizing.quality-verdict.run-id"]').fill(
      "deterministic-ui-run",
    );
    await page.locator('[data-evidence-id="rightsizing.quality-verdict.verdict"]').selectOption(
      "unknown",
    );
    await page.locator('[data-evidence-id="rightsizing.quality-verdict.source"]').fill(
      "human:deterministic-ui",
    );
    await page.locator('[data-evidence-id="rightsizing.quality-verdict.expected-output"]').fill(
      "Expected deterministic answer.",
    );
    await page.locator('[data-evidence-id="rightsizing.quality-verdict.detail"]').fill(
      "Configured only; no verdict submitted.",
    );

    const refreshPaths = [
      "/v1/econ/rightsizing/opportunities",
      "/v1/econ/unit-economics",
      "/v1/econ/waste",
    ];
    const refreshed = Promise.all(refreshPaths.map((path) => page.waitForResponse((response) => (
      response.request().method() === "GET" && new URL(response.url()).pathname === path
    ))));
    await page.locator('[data-evidence-id="rightsizing.action.refresh"]').click();
    const refreshResults = (await refreshed).map((response) => ({
      path: new URL(response.url()).pathname,
      status: response.status(),
    }));
    expect(refreshResults).toEqual(refreshPaths.map((path) => ({ path, status: 403 })));
    await expect(page.locator('[data-evidence-id="rightsizing.opportunities.error"]')).toContainText(
      "Metrics read permission is required",
    );

    const fieldValues = await Promise.all([
      "rightsizing.suggest.incumbent",
      "rightsizing.suggest.min-savings-pct",
      "rightsizing.suggest.limit",
      "rightsizing.experiment.node-id",
      "rightsizing.experiment.incumbent",
      "rightsizing.experiment.instruction",
      "rightsizing.experiment.tolerance-pct",
      "rightsizing.experiment.max-cases",
      "rightsizing.experiment.judge-model",
      "rightsizing.experiment.max-candidates",
      "rightsizing.experiment.min-cases",
      "rightsizing.quality-verdict.run-id",
      "rightsizing.quality-verdict.verdict",
      "rightsizing.quality-verdict.source",
      "rightsizing.quality-verdict.expected-output",
      "rightsizing.quality-verdict.detail",
    ].map(async (evidenceId) => ({
      evidence_id: evidenceId,
      value: await page.locator(`[data-evidence-id="${evidenceId}"]`).inputValue(),
    })));
    const checkedFields = await Promise.all([
      "rightsizing.suggest.needs-tools",
      "rightsizing.suggest.needs-vision",
      "rightsizing.experiment.needs-tools",
      "rightsizing.experiment.needs-vision",
    ].map(async (evidenceId) => ({
      evidence_id: evidenceId,
      checked: await page.locator(`[data-evidence-id="${evidenceId}"]`).isChecked(),
    })));

    const focusTargets = [
      "rightsizing.suggest.incumbent",
      "rightsizing.suggest.min-savings-pct",
      "rightsizing.suggest.limit",
      "rightsizing.experiment.node-id",
      "rightsizing.experiment.incumbent",
      "rightsizing.experiment.instruction",
      "rightsizing.experiment.tolerance-pct",
      "rightsizing.experiment.max-cases",
      "rightsizing.experiment.judge-model",
      "rightsizing.experiment.max-candidates",
      "rightsizing.experiment.min-cases",
      "rightsizing.quality-verdict.run-id",
      "rightsizing.quality-verdict.verdict",
      "rightsizing.quality-verdict.source",
      "rightsizing.quality-verdict.expected-output",
      "rightsizing.quality-verdict.detail",
    ];
    for (const evidenceId of focusTargets) {
      const control = page.locator(`[data-evidence-id="${evidenceId}"]`);
      await control.focus();
      expect(
        await control.evaluate((element) => element.matches(":focus-visible")),
        `${evidenceId} suppressed its focus ring`,
      ).toBe(true);
    }

    const axe = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    const axeViolations = axe.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.map((node) => node.target),
    }));
    await attachSafeJson(testInfo, "rightsizing-field-axe-wcag22-aa", axeViolations);
    expect(axeViolations).toEqual([]);
    const unexpectedConsoleErrors = consoleErrors.filter((message) => (
      message !== "Failed to load resource: the server responded with a status of 403 (Forbidden)"
    ));
    expect(consoleErrors).toHaveLength(6);
    expect(unexpectedConsoleErrors, unexpectedConsoleErrors.join("\n")).toEqual([]);
    await attachSafeJson(testInfo, "rightsizing-field-console-summary", {
      total_error_count: consoleErrors.length,
      expected_metrics_denial_resource_errors: consoleErrors.length - unexpectedConsoleErrors.length,
      unexpected_error_count: unexpectedConsoleErrors.length,
    });
    await browserEvidence.attach(testInfo);

    await testInfo.attach("rightsizing-field-contract", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "rightsizing-field-contract", {
      browser: testInfo.project.name,
      stable_evidence_ids: true,
      modes: ["equivalence", "correctness"],
      verdict_options: ["good", "bad", "unknown"],
      every_field_configured: true,
      field_values: fieldValues,
      checked_fields: checkedFields,
      static_validation_results: [
        {
          evidence_id: "rightsizing.suggest.min-savings-pct",
          message: "Minimum savings must be a number from 0 through 100.",
          cleared_on_valid_change: true,
        },
        {
          evidence_id: "rightsizing.suggest.limit",
          message: "Limit must be a whole number from 1 through 20.",
          cleared_on_valid_change: true,
        },
      ],
      validation_results: validationResults,
      refresh_results: refreshResults,
      static_provider_requests: staticRequests,
      measured_provider_requests: measuredRequests,
      paid_cost_observation: "no paid call was eligible because no measured request was sent; persistent ledger delta is recorded separately",
    });
  });

  test("rightsizing explains operator denial before any measured provider call", async ({ page }, testInfo) => {
    test.setTimeout(60_000);
    test.skip(testInfo.project.name !== "desktop-1440", "one role-denial pass is sufficient");
    test.skip(!operatorApiKey, "requires the disposable tenant operator credential");
    coverCriteria(testInfo, "rightsizing.field-contract", "identity-and-isolation.role-denial");
    await page.goto("/console/rightsizing/", { waitUntil: "networkidle" });

    await page.locator('[data-evidence-id="rightsizing.experiment.node-id"]').fill("research");
    await page.locator('[data-evidence-id="rightsizing.experiment.incumbent"]').fill("gpt-4o-mini");
    await page.locator('[data-evidence-id="rightsizing.experiment.instruction"]').fill(
      "Answer only from the provided evidence.",
    );
    const denialResponse = page.waitForResponse((response) => (
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith("/v1/econ/rightsizing/experiment")
    ));
    await page.locator('[data-evidence-id="rightsizing.experiment.submit"]').click();
    expect((await denialResponse).status()).toBe(403);
    await expect(page.locator('[data-evidence-id="rightsizing.experiment.error"]')).toContainText(
      "Running measured experiments requires Metrics admin permission.",
    );
    await testInfo.attach("rightsizing-operator-denial", {
      body: await page.screenshot({ fullPage: true, animations: "disabled" }),
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "rightsizing-operator-denial", {
      role: "operator",
      response_status: 403,
      denial_visible_in_page: true,
      provider_call_admitted: false,
    });
  });

  test("rightsizing remains operable at 200 percent zoom", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one zoom geometry pass is sufficient");
    coverCriteria(testInfo, "ui.zoom-200-percent", "ui.no-document-overflow");
    await page.goto("/console/rightsizing/", { waitUntil: "networkidle" });
    await page.evaluate(() => {
      document.documentElement.style.zoom = "2";
    });

    await expect(page.getByRole("button", { name: "Find cheaper models" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Run experiment" })).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);

    const screenshot = testInfo.outputPath("rightsizing-zoom-200.png");
    await page.screenshot({ path: screenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("rightsizing-zoom-200", { path: screenshot, contentType: "image/png" });
  });

  test("economics distinguishes provider spend, exposure, and provider-free run attribution", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one reconciliation pass is sufficient");
    coverCriteria(
      testInfo,
      "economics-and-rightsizing.result",
      "economics-and-rightsizing.reconciliation",
    );
    const response = await request.get(`${apiBase}/v1/tenants/${tenant}/cost`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    const ledger = await response.json() as {
      actual_spend_usd: number;
      active_exposure_usd: number;
      ambiguous_exposure_usd: number;
      synthetic_control_usd: number;
    };
    // This campaign now has an instrumented provider verification in its
    // persistent ledger. It must render the tenant's canonical measured value,
    // not the former all-zero fixture state.
    expect(ledger.actual_spend_usd).toBeGreaterThan(0);
    expect(ledger.active_exposure_usd).toBe(0);
    expect(ledger.ambiguous_exposure_usd).toBe(0);
    const displayedActual = `$${ledger.actual_spend_usd.toFixed(8).replace(/0+$/, "").replace(/\.$/, "")}`;

    const configurationResponse = await request.get(`${apiBase}/v1/econ/configuration`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(configurationResponse.status()).toBe(200);
    const configuration = await configurationResponse.json() as { failure_mode: string };
    expect(configuration.failure_mode).toBe("fail_closed");

    await page.goto("/console/cost/", { waitUntil: "networkidle" });
    await expect(page.getByText(displayedActual, { exact: true }).first()).toBeVisible();
    const exposureCard = page.getByRole("heading", { name: "Reserved exposure" }).locator("..");
    await expect(exposureCard).toContainText("$0.00");
    await expect(exposureCard).toContainText("$0.00 active · $0.00 ambiguous");
    await expect(page.getByText("Reserved or unresolved maxima · not yet spend", { exact: false })).toBeVisible();
    await expect(page.getByText(
      "Fail-closed: new provider spend is denied when Regulus cannot authorize it.",
      { exact: true },
    )).toBeVisible();
    await expect(page.getByText("Control proofs excluded", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Run-attributed economics" })).toBeVisible();
    await expect(page.getByText("No priced workflow runs in this window", { exact: true })).toBeVisible();
    await expect(page.getByText("Window / operation difference", { exact: true })).toBeVisible();
    const reconciliation = page.locator('[data-evidence-id="economics-run-ledger-reconciliation"]');
    await expect(reconciliation).toContainText(displayedActual);
    await expect(reconciliation).toContainText("$0.00");
    await expect(page.getByText(/other deployments have no recorded provider spend/)).toBeVisible();

    const unitResponse = await request.get(`${apiBase}/v1/econ/unit-economics?scope=tenant`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(unitResponse.status()).toBe(200);
    const unit = await unitResponse.json() as { total_cost_usd: number; runs_with_cost: number };
    expect(unit.total_cost_usd).toBe(0);
    expect(unit.runs_with_cost).toBe(0);

    const economicsScreenshot = testInfo.outputPath("economics-ledger-run-reconciliation.png");
    await page.screenshot({
      path: economicsScreenshot,
      fullPage: true,
      animations: "disabled",
    });
    await testInfo.attach("economics-ledger-run-reconciliation", {
      path: economicsScreenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "economics-ledger-reconciliation", {
      ledger_actual_usd: ledger.actual_spend_usd,
      run_attributed_usd: unit.total_cost_usd,
      difference_usd: ledger.actual_spend_usd - unit.total_cost_usd,
      active_exposure_usd: ledger.active_exposure_usd,
      ambiguous_exposure_usd: ledger.ambiguous_exposure_usd,
      synthetic_control_usd: ledger.synthetic_control_usd,
      failure_mode: configuration.failure_mode,
      interpretation: "operation-level deployment spend is not assigned to workflow runs",
    });
  });

  test("Regulus distinguishes explicit valuation from the production spend ledger", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one valuation-state pass is sufficient");
    coverCriteria(testInfo, "economics-and-rightsizing.regulus-valuation-state");

    const response = await request.get(`${apiBase}/v1/econ/regulus/dashboard/kpis`, {
      headers: { "X-API-Key": apiKey! },
    });
    expect(response.status()).toBe(200);
    const kpis = await response.json() as {
      total_ai_spend_usd: number;
      total_ai_value_usd: number;
      net_ai_margin_usd: number;
    };
    expect(kpis.total_ai_spend_usd).toBe(0);
    expect(kpis.total_ai_value_usd).toBe(0);
    expect(kpis.net_ai_margin_usd).toBe(0);

    await page.goto("/console/regulus/", { waitUntil: "networkidle" });
    await expect(page.getByText("Valuation model, not the spend ledger", { exact: true })).toBeVisible();
    await expect(page.getByText("Valued execution cost", { exact: true })).toBeVisible();
    await expect(page.getByText("Recorded outcome value", { exact: true })).toBeVisible();
    await expect(page.getByText("$0.00", { exact: true })).toHaveCount(3);
    await expect(page.getByText("No synthetic or measured outcomes", { exact: false })).toBeVisible();
    const regulusScreenshot = testInfo.outputPath("regulus-explicit-valuation-empty-state.png");
    await page.screenshot({
      path: regulusScreenshot,
      fullPage: true,
      animations: "disabled",
    });
    await testInfo.attach("regulus-explicit-valuation-empty-state", {
      path: regulusScreenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "regulus-valuation-state", {
      ...kpis,
      production_ledger_source: "separate /v1/tenants/{tenant_id}/cost endpoint",
      outcome_valuation_state: "not_recorded",
    });
  });

  test("Regulus capability registry and enforcement references stay linked", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one registry-linkage pass is sufficient");
    coverCriteria(testInfo, "economics-and-rightsizing.capabilities-linkage");
    const headers = { "X-API-Key": apiKey! };
    const [capabilityResponse, enforcementResponse] = await Promise.all([
      request.get(`${apiBase}/v1/econ/regulus/registry/capabilities`, { headers }),
      request.get(`${apiBase}/v1/econ/regulus/enforcement/actions`, { headers }),
    ]);
    expect(capabilityResponse.status()).toBe(200);
    expect(enforcementResponse.status()).toBe(200);
    const capabilities = await capabilityResponse.json() as Array<{
      id: string;
      name: string;
      tenant_id: string;
    }>;
    const actions = await enforcementResponse.json() as Array<{ capability_id: string }>;
    const ids = new Set(capabilities.map((capability) => capability.id));
    expect(capabilities.map((capability) => capability.name)).toEqual(
      expect.arrayContaining(["Demo agent", "research"]),
    );
    expect(actions.length).toBeGreaterThan(0);
    expect(actions.every((action) => ids.has(action.capability_id))).toBe(true);

    await page.goto("/console/regulus/capabilities/", { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /research/ }).click();
    await expect(page.getByText("Zeroth agent node research in deployment", { exact: false })).toBeVisible();
    await expect(
      page.locator("p").filter({ hasText: "evaluation-studio-v1-grounded-researcher-v1" }),
    ).toBeVisible();
    await expect(page.getByText("No implementation comparisons yet.", { exact: true })).toBeVisible();
    await expect(page.getByText("No evaluation runs yet.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Show history" }).click();
    await expect(page.getByText("No evaluation history yet.", { exact: true })).toBeVisible();

    const screenshot = testInfo.outputPath("capability-research-enforcement-linkage.png");
    await page.screenshot({ path: screenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("capability-research-enforcement-linkage", {
      path: screenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "capability-enforcement-linkage", {
      tenant,
      capabilities: capabilities.map(({ id, name, tenant_id }) => ({ id, name, tenant_id })),
      enforcement_capability_ids: [...new Set(actions.map((action) => action.capability_id))],
      orphaned_enforcement_references: actions
        .map((action) => action.capability_id)
        .filter((id) => !ids.has(id)),
    });
  });

  test("Cost-model lookups and reconciliation expose honest empty boundaries", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one cost-model boundary pass is sufficient");
    coverCriteria(
      testInfo,
      "economics-and-rightsizing.cost-model-boundaries",
      "economics-and-rightsizing.reconciliation-empty-state",
    );
    const headers = { "X-API-Key": apiKey! };

    await page.goto("/console/regulus/costing/", { waitUntil: "networkidle" });
    const profileInput = page.getByRole("textbox", { name: "Profile id" });
    const capabilityInput = page.getByRole("textbox", { name: "Capability id" });
    const fetchButtons = page.getByRole("button", { name: "Fetch", exact: true });

    await fetchButtons.nth(0).click();
    await expect(page.getByText("Enter a profile id first.", { exact: true })).toBeVisible();
    await profileInput.fill("99999999");
    await fetchButtons.nth(0).click();
    await expect(page.getByText('No cost profile for id "99999999".', { exact: true })).toBeVisible();

    const capabilityResponse = await request.get(`${apiBase}/v1/econ/regulus/registry/capabilities`, { headers });
    expect(capabilityResponse.status()).toBe(200);
    const capabilities = await capabilityResponse.json() as Array<{ id: string; name: string }>;
    const research = capabilities.find((capability) => capability.name === "research");
    expect(research).toBeDefined();
    await capabilityInput.fill(research!.id);
    await fetchButtons.nth(1).click();
    await expect(
      page.getByText(`No cost estimate for capability "${research!.id}".`, { exact: true }),
    ).toBeVisible();

    const costingScreenshot = testInfo.outputPath("cost-model-empty-boundaries.png");
    await page.screenshot({ path: costingScreenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("cost-model-empty-boundaries", {
      path: costingScreenshot,
      contentType: "image/png",
    });

    const calibrationResponse = await request.get(
      `${apiBase}/v1/econ/regulus/reconciliation/calibration-summary`,
      { headers },
    );
    expect(calibrationResponse.status()).toBe(200);
    expect(await calibrationResponse.json()).toEqual([]);
    await page.goto("/console/regulus/reconciliation/", { waitUntil: "networkidle" });
    await expect(page.getByText("No calibration data yet", { exact: false })).toBeVisible();
    await expect(page.getByText("No calibration history yet.", { exact: true })).toBeVisible();
    const reconciliationScreenshot = testInfo.outputPath("reconciliation-empty-state.png");
    await page.screenshot({ path: reconciliationScreenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("reconciliation-empty-state", {
      path: reconciliationScreenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "costing-reconciliation-boundaries", {
      unknown_profile_id: "99999999",
      research_capability_id: research!.id,
      cost_estimate_state: "not_recorded",
      calibration_rows: 0,
      interpretation: "lookups are operational; no profile, estimate, or ground truth is fabricated",
    });
  });

  test("Enforcement approval persists a reason and linked policy state without provider spend", async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one governed decision pass is sufficient");
    coverCriteria(testInfo, "economics-and-rightsizing.enforcement-approval");
    const headers = { "X-API-Key": apiKey! };
    const beforeActionsResponse = await request.get(`${apiBase}/v1/econ/regulus/enforcement/actions`, { headers });
    const beforePoliciesResponse = await request.get(`${apiBase}/v1/econ/regulus/enforcement/policy-actions`, { headers });
    const beforeCostResponse = await request.get(`${apiBase}/v1/tenants/${tenant}/cost`, { headers });
    expect(beforeActionsResponse.status()).toBe(200);
    expect(beforePoliciesResponse.status()).toBe(200);
    expect(beforeCostResponse.status()).toBe(200);
    const beforeActions = await beforeActionsResponse.json() as Array<{
      id: number;
      status: string;
      capability_id: string;
      reason: string | null;
    }>;
    const reason = "Synthetic UI acceptance: campaign evidence reviewed; no production policy change.";
    const pending = beforeActions.find((action) => action.status === "pending");
    const alreadyApproved = beforeActions.find((action) => (
      action.status === "approved" && action.reason === reason
    ));
    const target = pending ?? alreadyApproved;
    expect(target, "campaign fixture must be pending or retain the accepted decision").toBeDefined();
    if (pending) expect(pending.reason).toContain("[SYNTHETIC DEMO]");
    const beforePolicies = await beforePoliciesResponse.json() as Array<{
      id: number;
      capability_id: string;
      status: string;
    }>;
    const beforeCost = await beforeCostResponse.json() as { actual_spend_usd: number };

    await page.goto("/console/regulus/enforcement/", { waitUntil: "networkidle" });
    const card = page.locator(`[data-evidence-scope="enforcement-${target!.id}"]`);
    await expect(card).toBeVisible();
    if (pending) {
      await card.getByRole("textbox", { name: "Decision reason" }).fill(reason);
      const beforeScreenshot = testInfo.outputPath("enforcement-pending-configured.png");
      await page.screenshot({ path: beforeScreenshot, fullPage: true, animations: "disabled" });
      await testInfo.attach("enforcement-pending-configured", {
        path: beforeScreenshot,
        contentType: "image/png",
      });
      page.once("dialog", (dialog) => dialog.accept());
      await card.getByRole("button", { name: "Approve", exact: true }).click();
    } else {
      const prior = process.env.ZEROTH_EVALUATION_ENFORCEMENT_BEFORE_SCREENSHOT;
      if (prior && existsSync(prior)) {
        await testInfo.attach("enforcement-pending-configured", {
          path: prior,
          contentType: "image/png",
        });
      }
    }
    await expect(card.getByText("approved", { exact: true }).first()).toBeVisible();
    await expect(card.getByText(reason, { exact: true })).toBeVisible();

    const [afterActionsResponse, afterPoliciesResponse, afterCostResponse] = await Promise.all([
      request.get(`${apiBase}/v1/econ/regulus/enforcement/actions`, { headers }),
      request.get(`${apiBase}/v1/econ/regulus/enforcement/policy-actions`, { headers }),
      request.get(`${apiBase}/v1/tenants/${tenant}/cost`, { headers }),
    ]);
    const afterActions = await afterActionsResponse.json() as typeof beforeActions;
    const afterPolicies = await afterPoliciesResponse.json() as typeof beforePolicies;
    const afterCost = await afterCostResponse.json() as typeof beforeCost;
    const approved = afterActions.find((action) => action.id === target!.id);
    const linkedPolicy = afterPolicies.find((policy) => (
      policy.id === target!.id && policy.capability_id === target!.capability_id
    ));
    expect(approved?.status).toBe("approved");
    expect(approved?.reason).toBe(reason);
    expect(linkedPolicy?.status).toBe("APPLIED");
    expect(afterCost.actual_spend_usd).toBe(beforeCost.actual_spend_usd);

    const afterScreenshot = testInfo.outputPath("enforcement-approved-persisted.png");
    await page.screenshot({ path: afterScreenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("enforcement-approved-persisted", {
      path: afterScreenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "enforcement-approval-result", {
      action_id: target!.id,
      capability_id: target!.capability_id,
      action_status_before: pending?.status ?? "approved_persisted_after_prior_ui_submission",
      action_status_after: approved?.status,
      policy_status_before: beforePolicies.find((policy) => policy.id === target!.id)?.status ?? null,
      policy_status_after: linkedPolicy?.status ?? null,
      decision_reason: reason,
      actual_spend_before_usd: beforeCost.actual_spend_usd,
      actual_spend_after_usd: afterCost.actual_spend_usd,
    });
  });

  test("measured rightsizing stops at the no-traffic gate without provider spend", async ({ page, request }, testInfo) => {
    test.skip(
      !["desktop-1440", "webkit-1440"].includes(testInfo.project.name),
      "one measured-state pass per required browser engine is sufficient",
    );
    coverCriteria(testInfo, "economics-and-rightsizing.rightsizing-no-traffic-gate");
    const headers = { "X-API-Key": apiKey! };
    const before = await (await request.get(`${apiBase}/v1/tenants/${tenant}/cost`, { headers })).json();
    await page.goto("/console/rightsizing/", { waitUntil: "networkidle" });

    await page.getByRole("textbox", { name: /node_id The agent node/ }).fill("research");
    await page.getByRole("textbox", { name: /incumbent The model it runs/ }).fill("gpt-4o-mini");
    await page.getByRole("textbox", { name: /instruction The agent's system prompt/ }).fill(
      "Answer only from retrieved sources.",
    );
    await page.getByRole("button", { name: "Run experiment" }).click();
    await expect(page.getByText("No tool-free successful runs on record", { exact: false })).toBeVisible();

    const after = await (await request.get(`${apiBase}/v1/tenants/${tenant}/cost`, { headers })).json();
    expect(after.actual_spend_usd).toBe(before.actual_spend_usd);
    expect(after.paid_spend_usd).toBe(before.paid_spend_usd);
    expect(after.budget_consumed_usd).toBe(before.budget_consumed_usd);
    const screenshot = testInfo.outputPath("rightsizing-measured-no-traffic.png");
    await page.screenshot({ path: screenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("rightsizing-measured-no-traffic", {
      path: screenshot,
      contentType: "image/png",
    });
    await attachSafeJson(testInfo, "rightsizing-measured-no-traffic", {
      node_id: "research",
      cases: 0,
      provider_calls_made: 0,
      ledger_unchanged: true,
      actual_spend_before_usd: before.actual_spend_usd,
      actual_spend_after_usd: after.actual_spend_usd,
      budget_consumed_before_usd: before.budget_consumed_usd,
      budget_consumed_after_usd: after.budget_consumed_usd,
    });
  });

  test("audit chain verification succeeds with signed deployment evidence", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one signed verification pass is sufficient");
    coverCriteria(
      testInfo,
      "audit.workflow-default-view",
      "audit.metadata-only-presentation",
      "audit.signed-chain-verification",
    );
    await page.goto("/console/audit/", { waitUntil: "networkidle" });
    await expect(page.getByText("Payload values are withheld", { exact: false })).toBeVisible();
    await expect(page.getByText("***REDACTED***", { exact: true })).toHaveCount(0);
    const configuredScreenshot = testInfo.outputPath("audit-configured.png");
    await page.screenshot({ path: configuredScreenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("audit-configured", { path: configuredScreenshot, contentType: "image/png" });
    const [verificationResponse] = await Promise.all([
      page.waitForResponse((response) => (
        response.url().includes("/audit-verification") && response.status() === 200
      )),
      page.getByRole("button", { name: "Verify chain" }).click(),
    ]);
    const verification = await verificationResponse.json() as {
      scope: string;
      verified: boolean;
      record_count: number;
      signature_verified: boolean | null;
      signing_key_id: string | null;
      unsigned_record_count: number;
    };
    expect(verification.verified).toBe(true);
    expect(verification.signature_verified).toBe(true);
    expect(verification.unsigned_record_count).toBe(0);
    await attachSafeJson(testInfo, "audit-chain-verification", verification);
    await expect(page.getByText("chain intact · signatures valid", { exact: true })).toBeVisible();
    const verifiedScreenshot = testInfo.outputPath("audit-chain-verified.png");
    await page.screenshot({ path: verifiedScreenshot, fullPage: true, animations: "disabled" });
    await testInfo.attach("audit-chain-verified", { path: verifiedScreenshot, contentType: "image/png" });
  });

  test("core dashboards reflow without page-level horizontal overflow", async ({ page }, testInfo) => {
    test.setTimeout(180_000);
    coverCriteria(
      testInfo,
      "cross-cutting.incumbent-routes-reachable",
      "cross-cutting.responsive-no-page-overflow",
      "cross-cutting.no-console-or-api-errors",
    );
    const paths = [
      "/",
      "/runs",
      "/approvals",
      "/audit",
      "/deployments",
      "/artifacts",
      "/studio",
      "/templates",
      "/connectors",
      "/webhooks",
      "/cost",
      "/regulus/capabilities",
      "/regulus/enforcement",
      "/regulus/reconciliation",
      "/retention",
      "/rightsizing",
      "/metrics",
    ] as const;
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    const failedApiResponses: string[] = [];
    const failedResponses: string[] = [];
    const failedRequests: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") {
        const location = message.location().url;
        consoleErrors.push(`${message.text()}${location ? ` @ ${location}` : ""}`);
      }
    });
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("requestfailed", (request) => {
      failedRequests.push(`${request.failure()?.errorText ?? "request failed"} ${request.url()}`);
    });
    page.on("response", (response) => {
      const url = response.url();
      if (response.status() >= 400) {
        failedResponses.push(`${response.status()} ${url}`);
      }
      if (response.status() >= 400 && (url.includes("/v1/") || url.includes("/api/studio/"))) {
        failedApiResponses.push(`${response.status()} ${new URL(url).pathname}`);
      }
    });
    for (const path of paths) {
      const route = path === "/" ? "/console/" : `/console${path}/`;
      await page.goto(route, { waitUntil: "networkidle" });
      await expect(page.locator("main")).toBeVisible();
      const dimensions = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(dimensions.scrollWidth, `${path} overflowed at ${testInfo.project.name}`).toBeLessThanOrEqual(
        dimensions.clientWidth + 1,
      );
      const slug = path === "/" ? "overview" : path.slice(1).replaceAll("/", "-");
      await page.screenshot({
        path: testInfo.outputPath(`${slug}-${testInfo.project.name}.png`),
        fullPage: true,
        animations: "disabled",
      });
    }
    expect(failedApiResponses, failedApiResponses.join("\n")).toEqual([]);
    expect(failedResponses, failedResponses.join("\n")).toEqual([]);
    expect(failedRequests, failedRequests.join("\n")).toEqual([]);
    expect(pageErrors, pageErrors.join("\n")).toEqual([]);
    expect(consoleErrors, consoleErrors.join("\n")).toEqual([]);
  });

  test("loop architecture exposes its bounded retry contract without a redundant loop-back chip", async ({ page }, testInfo) => {
    const consoleErrors: string[] = [];
    const failedApiResponses: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    page.on("response", (response) => {
      const url = response.url();
      if (response.status() >= 400 && (url.includes("/v1/") || url.includes("/api/studio/"))) {
        failedApiResponses.push(`${response.status()} ${new URL(url).pathname}`);
      }
    });

    await page.goto(`/console/studio/edit/?id=${incidentLoopWorkflowId}`, { waitUntil: "networkidle" });
    await expect(page.getByLabel("Workflow name")).toHaveValue("Incident readiness review — local manifests");
    await expect(page.getByText("Prepare until ready", { exact: true })).toBeVisible();
    await expect(page.getByText("1 attempt + 2 retries", { exact: true })).toBeVisible();
    await expect(page.getByText("Repeat", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Done", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Limit", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Loop back", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Run code or a tool", { exact: true }).first()).toBeVisible();

    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
    expect(failedApiResponses, failedApiResponses.join("\n")).toEqual([]);
    expect(consoleErrors, consoleErrors.join("\n")).toEqual([]);
    await page.screenshot({
      path: testInfo.outputPath(`loop-architecture-${testInfo.project.name}.png`),
      fullPage: true,
      animations: "disabled",
    });
  });

  test("governed action graph exposes approval, cancellation, and local action stages", async ({ page }, testInfo) => {
    const failedApiResponses: string[] = [];
    page.on("response", (response) => {
      const url = response.url();
      if (response.status() >= 400 && (url.includes("/v1/") || url.includes("/api/studio/"))) {
        failedApiResponses.push(`${response.status()} ${new URL(url).pathname}`);
      }
    });

    await page.goto(`/console/studio/edit/?id=${approvalWorkflowId}`, { waitUntil: "networkidle" });
    await expect(page.getByLabel("Workflow name")).toHaveValue(
      "Evaluation governed remediation — corrected routing",
    );
    await expect(page.getByText("approval", { exact: true })).toBeVisible();
    await expect(page.getByText("evaluation-pre-action-barrier", { exact: true })).toBeVisible();
    await expect(page.getByText("synthetic-action", { exact: true })).toBeVisible();
    await expect(page.getByText("Pause for human sign-off", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Evaluation behavior == 'cancel after approval'", { exact: true })).toBeVisible();
    await expect(page.getByText("Run code or a tool", { exact: true })).toBeVisible();

    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
    expect(failedApiResponses, failedApiResponses.join("\n")).toEqual([]);
    await page.screenshot({
      path: testInfo.outputPath(`approval-action-architecture-${testInfo.project.name}.png`),
      fullPage: true,
      animations: "disabled",
    });
  });

  test("Overview stacks full-width operations below the horizontal setup strip", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one intermediate-width geometry pass is sufficient");
    await page.setViewportSize({ width: 2054, height: 970 });
    await page.goto("/console/", { waitUntil: "networkidle" });

    const primary = page.locator(".overview-primary-column");
    const deployment = primary.locator(":scope > section").first();
    const checklist = page.locator(".overview-content-stack > section").first();
    const [primaryBox, deploymentBox, checklistBox] = await Promise.all([
      primary.boundingBox(),
      deployment.boundingBox(),
      checklist.boundingBox(),
    ]);

    expect(primaryBox).not.toBeNull();
    expect(deploymentBox).not.toBeNull();
    expect(checklistBox).not.toBeNull();
    expect(deploymentBox!.width).toBeLessThanOrEqual(primaryBox!.width + 1);
    expect(Math.abs(deploymentBox!.x - checklistBox!.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(deploymentBox!.width - checklistBox!.width)).toBeLessThanOrEqual(1);
    expect(checklistBox!.y + checklistBox!.height).toBeLessThanOrEqual(deploymentBox!.y);

    for (const button of await deployment.getByRole("button", { name: "Rollback" }).all()) {
      const buttonBox = await button.boundingBox();
      expect(buttonBox).not.toBeNull();
      expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(deploymentBox!.x + deploymentBox!.width);
    }

    await page.screenshot({
      path: testInfo.outputPath("overview-no-overlap.png"),
      fullPage: true,
      animations: "disabled",
    });
  });

  test("previously accepted dashboard layout fixes remain cumulative", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-1440", "one cumulative geometry pass is sufficient");

    // 942 CSS pixels reproduces the user's 1884px-wide Safari capture at 200% zoom.
    await page.setViewportSize({ width: 942, height: 844 });
    await page.goto("/console/cost/", { waitUntil: "networkidle" });

    const band = page.locator("[class*='controlBand']");
    const cells = band.locator(":scope > section");
    await expect(cells).toHaveCount(4);
    const [bandBox, cellBoxes] = await Promise.all([
      band.boundingBox(),
      cells.evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().toJSON())),
    ]);
    expect(bandBox).not.toBeNull();
    expect(new Set(cellBoxes.map((box) => Math.round(box.y))).size).toBe(2);
    for (const rowY of new Set(cellBoxes.map((box) => Math.round(box.y)))) {
      const row = cellBoxes.filter((box) => Math.round(box.y) === rowY);
      expect(row).toHaveLength(2);
      expect(Math.max(...row.map((box) => box.right))).toBeCloseTo(bandBox!.x + bandBox!.width, 0);
    }

    const budgetInput = page.getByRole("textbox", { name: "Budget cap in USD" });
    const setButton = page.getByRole("button", { name: "Set", exact: true });
    const [inputBox, setBox] = await Promise.all([budgetInput.boundingBox(), setButton.boundingBox()]);
    expect(inputBox).not.toBeNull();
    expect(setBox).not.toBeNull();
    expect(setBox!.height).toBeCloseTo(inputBox!.height, 0);

    await expect(page.getByText("$0.0100", { exact: true })).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath("economics-200-percent-cumulative.png"),
      fullPage: true,
      animations: "disabled",
    });

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("/console/studio/", { waitUntil: "networkidle" });

    const workflowInput = page.getByRole("textbox", { name: "Workflow name" });
    const createButton = page.getByRole("button", { name: "Create", exact: true });
    await workflowInput.scrollIntoViewIfNeeded();
    const [workflowInputBox, createButtonBox] = await Promise.all([
      workflowInput.boundingBox(),
      createButton.boundingBox(),
    ]);
    expect(workflowInputBox).not.toBeNull();
    expect(createButtonBox).not.toBeNull();
    expect(createButtonBox!.y).toBeCloseTo(workflowInputBox!.y, 0);
    await page.screenshot({
      path: testInfo.outputPath("studio-create-form-alignment.png"),
      fullPage: false,
      animations: "disabled",
    });

    await page.getByRole("link", { name: /Customer data quality repair — local manifests/ }).click();
    await expect(page.locator(".studio-editor-left-panel")).toBeVisible();
    const [editorBox, backBox] = await Promise.all([
      page.locator(".studio-editor-shell").boundingBox(),
      page.locator(".studio-editor-back").boundingBox(),
    ]);
    const editorGeometry = await page.locator(".studio-editor-left-panel").evaluate((panel) => ({
      panelLeft: getComputedStyle(panel).left,
      panelMargin: getComputedStyle(panel).margin,
      shellClass: document.querySelector(".console-shell")?.className,
      viewportWidth: window.innerWidth,
    }));
    expect(editorBox).not.toBeNull();
    expect(backBox).not.toBeNull();
    expect(
      backBox!.x - editorBox!.x,
      JSON.stringify(editorGeometry),
    ).toBeLessThanOrEqual(20);

    await page.screenshot({
      path: testInfo.outputPath("studio-cumulative-spacing.png"),
      fullPage: true,
      animations: "disabled",
    });
  });
});
