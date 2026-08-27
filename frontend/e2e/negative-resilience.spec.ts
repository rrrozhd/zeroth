import { expect, test } from "@playwright/test";

import {
  assertDocumentLoaded,
  assertKeyboardFocus,
  attachSafeJson,
  BrowserEvidence,
  configurePage,
  extractEvidenceIdentities,
  workflowFixture,
} from "./support/live-evaluation";
import {
  ScenarioController,
  type PreparedScenario,
  type ScenarioDefinition,
} from "./support/scenario-controller";

const definitions: ScenarioDefinition[] = [
  { id: "w1_empty_query", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w1_oversized_query", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w1_no_result", workflow: 1, expectation: { runStatus: "completed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w1_conflicting_documents", workflow: 1, expectation: { runStatus: "completed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w1_bad_credential", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, deterministicProviderFault: true },
  { id: "w1_provider_timeout", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, deterministicProviderFault: true },
  { id: "w1_rate_limit", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, deterministicProviderFault: true },
  { id: "w1_malformed_response", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, deterministicProviderFault: true },
  { id: "w1_excessive_revision", workflow: 1, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w2_empty_batch", workflow: 2, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w2_over_24_batch", workflow: 2, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w2_malformed_item", workflow: 2, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 } },
  { id: "w2_retrieval_miss", workflow: 2, expectation: { runStatus: "completed", markerCount: 0, reexecutionCount: 0, partialCollectionCount: 7 } },
  { id: "w2_cancellation", workflow: 2, expectation: { runStatus: "cancelled", markerCount: 0, reexecutionCount: 0 }, uiAction: "cancel" },
  { id: "w2_refresh_restoration", workflow: 2, expectation: { runStatus: "completed", markerCount: 0, reexecutionCount: 0, partialCollectionCount: 8 }, uiAction: "refresh" },
  { id: "w2_child_pause_partial", workflow: 2, expectation: { runStatus: "paused", markerCount: 0, reexecutionCount: 0, partialCollectionCount: 7 } },
  { id: "w2_child_failure_partial", workflow: 2, expectation: { runStatus: "completed", markerCount: 0, reexecutionCount: 0, partialCollectionCount: 7 } },
  { id: "w3_rejection", workflow: 3, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, uiAction: "reject" },
  { id: "w3_refresh_before_approval", workflow: 3, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, uiAction: "refresh_reject" },
  { id: "w3_sla_expiry", workflow: 3, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, checkpoint: "advance_sla" },
  { id: "w3_duplicate_submission", workflow: 3, expectation: { runStatus: "completed", markerCount: 1, reexecutionCount: 0, operationStatus: "completed" }, uiAction: "approve", checkpoint: "duplicate_submission" },
  { id: "w3_cancellation_after_approval", workflow: 3, expectation: { runStatus: "cancelled", markerCount: 0, reexecutionCount: 0 }, uiAction: "approve" },
  { id: "w3_restart_before_receipt", workflow: 3, expectation: { runStatus: "completed", markerCount: 1, reexecutionCount: 0, operationStatus: "completed" }, uiAction: "approve", checkpoint: "restart_before_receipt" },
  { id: "w3_restart_after_receipt", workflow: 3, expectation: { runStatus: "completed", markerCount: 1, reexecutionCount: 0, operationStatus: "completed" }, uiAction: "approve", checkpoint: "restart_after_receipt" },
  { id: "w3_sink_unavailable", workflow: 3, expectation: { runStatus: "failed", markerCount: 0, reexecutionCount: 0 }, uiAction: "approve" },
  { id: "w3_timeout_after_commit", workflow: 3, expectation: { runStatus: "completed", markerCount: 1, reexecutionCount: 0, operationStatus: "completed" }, uiAction: "approve" },
  { id: "w3_ambiguous_no_reexecution", workflow: 3, expectation: { runStatus: "failed", markerCount: 1, reexecutionCount: 0, operationStatus: "ambiguous" }, uiAction: "approve" },
];

const liveEnabled = process.env.ZEROTH_EVALUATION_LIVE === "1";
const apiBase = process.env.ZEROTH_EVALUATION_API_BASE ?? "http://127.0.0.1:8120";
const apiOrigin = new URL(apiBase).origin;
const tenant = process.env.ZEROTH_EVALUATION_TENANT ?? "evaluation-studio-v1";
const apiKey = process.env.ZEROTH_EVALUATION_API_KEY;
const controllerUrl = process.env.ZEROTH_EVALUATION_FAULT_CONTROLLER_URL;

function scenarioGate(id: string): string {
  return `ZEROTH_EVALUATION_SCENARIO_${id.toUpperCase()}`;
}

async function submitScenario(page: Parameters<typeof assertDocumentLoaded>[0], workflowId: string, prepared: PreparedScenario) {
  await assertDocumentLoaded(page, `/studio/edit?id=${encodeURIComponent(workflowId)}`);
  await page.getByRole("button", { name: "Run", exact: true }).first().click();
  await page.getByLabel("Input payload (JSON)").fill(JSON.stringify(prepared.input_payload));
  await page.getByRole("button", { name: "Run", exact: true }).last().click();
}

async function resolveApproval(page: Parameters<typeof assertDocumentLoaded>[0], prepared: PreparedScenario, decision: "Approve" | "Reject") {
  expect(prepared.approval_node_id, "controller must expose the pending approval node").toBeTruthy();
  await assertDocumentLoaded(page, "/approvals");
  const node = page.getByText(prepared.approval_node_id!, { exact: true });
  await expect(node).toBeVisible({ timeout: 60_000 });
  const card = node.locator("xpath=ancestor::*[.//button[normalize-space()='Approve']][1]");
  await card.getByRole("button", { name: decision, exact: true }).click();
}

async function visibleStudioRunId(
  page: Parameters<typeof assertDocumentLoaded>[0],
): Promise<string> {
  const identity = page.locator('[data-evidence-id="studio.run.current-id"]');
  await expect(identity).toBeVisible({ timeout: 60_000 });
  const runId = (await identity.textContent())?.trim();
  expect(runId, "Studio must expose the exact current run identity").toBeTruthy();
  return runId!;
}

test.describe("live negative and resilience scenarios", () => {
  test.skip(!liveEnabled, "requires explicit live evaluation mode");

  for (const definition of definitions) {
    test(`${definition.id} has exact fail-closed evidence`, async ({ page, request }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop-1440", "stateful fault scenarios run once; viewport coverage is separate");
      test.skip(process.env[scenarioGate(definition.id)] !== "1", `requires ${scenarioGate(definition.id)}=1`);
      test.skip(
        process.env.ZEROTH_EVALUATION_ALLOW_NEGATIVE_RUNS !== "I_ACKNOWLEDGE_BOUNDED_NEGATIVE_RUNS",
        "negative runs require the exact bounded-run acknowledgement",
      );
      test.skip(!controllerUrl, "required fault/restart controller fixture is not configured");
      test.skip(!apiKey, "required evaluation service credential fixture is not configured");
      const fixture = workflowFixture(definition.workflow);
      test.skip(!fixture, `workflow ${definition.workflow} fixture environment is incomplete`);
      await configurePage(page, apiBase, tenant, apiKey!);

      let serviceHealthy = false;
      try {
        const health = await request.get(`${apiBase}/health`, {
          headers: { "X-API-Key": apiKey!, "X-Tenant-ID": tenant },
          timeout: 3_000,
        });
        serviceHealthy = health.status() >= 200 && health.status() < 300;
      } catch {
        serviceHealthy = false;
      }
      test.skip(!serviceHealthy, "required evaluation service fixture is unavailable");

      const controller = new ScenarioController(
        request,
        controllerUrl!,
        process.env.ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY,
      );
      test.skip(!(await controller.available()), "required fault/restart controller fixture is unavailable");
      const evidence = new BrowserEvidence(page, apiOrigin);
      const prepared = await controller.prepare(definition, fixture!.id);

      try {
        await submitScenario(page, fixture!.id, prepared);
        if (definition.uiAction === "refresh") {
          const before = await controller.checkpoint(prepared.fixture_id, "refresh_before");
          const beforeUiRunId = await visibleStudioRunId(page);
          expect(before.run_id).toBe(beforeUiRunId);
          await page.reload({ waitUntil: "networkidle" });
          await assertKeyboardFocus(page, testInfo);
          const restoredUiRunId = await visibleStudioRunId(page);
          const after = await controller.checkpoint(prepared.fixture_id, "refresh_after");
          expect(after.run_id).toBe(restoredUiRunId);
          expect(restoredUiRunId).toBe(beforeUiRunId);
          await attachSafeJson(testInfo, "refresh-restoration", {
            before: { ...before, ui_run_id: beforeUiRunId },
            after: { ...after, ui_run_id: restoredUiRunId },
          });
        } else if (definition.uiAction === "refresh_reject") {
          const before = await controller.checkpoint(prepared.fixture_id, "refresh_before");
          const beforeUiRunId = await visibleStudioRunId(page);
          expect(before.run_id).toBe(beforeUiRunId);
          await page.reload({ waitUntil: "networkidle" });
          await assertKeyboardFocus(page, testInfo);
          const restoredUiRunId = await visibleStudioRunId(page);
          const after = await controller.checkpoint(prepared.fixture_id, "refresh_after");
          expect(after.run_id).toBe(restoredUiRunId);
          expect(restoredUiRunId).toBe(beforeUiRunId);
          await attachSafeJson(testInfo, "refresh-restoration", {
            before: { ...before, ui_run_id: beforeUiRunId },
            after: { ...after, ui_run_id: restoredUiRunId },
          });
          await resolveApproval(page, prepared, "Reject");
        } else if (definition.uiAction === "cancel") {
          await controller.checkpoint(prepared.fixture_id, "run_submitted");
          await assertDocumentLoaded(page, "/runs");
          page.once("dialog", (dialog) => dialog.accept());
          await page.getByRole("button", { name: "Cancel", exact: true }).first().click();
        } else if (definition.uiAction === "approve" || definition.uiAction === "reject") {
          await resolveApproval(page, prepared, definition.uiAction === "approve" ? "Approve" : "Reject");
        }

        if (definition.id === "w3_cancellation_after_approval") {
          await controller.checkpoint(prepared.fixture_id, "approval_resolved");
          await assertDocumentLoaded(page, "/runs");
          page.once("dialog", (dialog) => dialog.accept());
          await page.getByRole("button", { name: "Cancel", exact: true }).first().click();
        }
        if (definition.checkpoint === "restart_before_receipt") {
          await controller.coordinateRestart(prepared.fixture_id, "before_receipt");
        } else if (definition.checkpoint === "restart_after_receipt") {
          await controller.coordinateRestart(prepared.fixture_id, "after_receipt");
        } else if (definition.checkpoint) {
          await controller.checkpoint(prepared.fixture_id, definition.checkpoint);
        }

        const verified = await controller.verify(prepared.fixture_id, definition.expectation);
        await attachSafeJson(testInfo, "scenario-verification", {
          scenario_id: definition.id,
          expected: definition.expectation,
          actual: {
            run_status: verified.run_status,
            marker_count: verified.marker_count,
            reexecution_count: verified.reexecution_count,
            partial_collection_count: verified.partial_collection_count,
            operation_status: verified.operation_status,
          },
          identity: extractEvidenceIdentities(verified),
        });
        evidence.assertNoFailedApiResponses();
        await evidence.attach(testInfo);
      } finally {
        await controller.cleanup(prepared.fixture_id);
      }
    });
  }
});
