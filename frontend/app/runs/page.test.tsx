// @vitest-environment jsdom

import type { AnchorHTMLAttributes } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getChildRuns: vi.fn(),
  getRunTimeline: vi.fn(),
  getRunEvidence: vi.fn(),
  getHealth: vi.fn(),
  getInputContract: vi.fn(),
  cancelRun: vi.fn(),
  resolveAmbiguousOperation: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams("run=run-1"),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import RunsPage from "./page";

const runningRun = {
  run_id: "run-1",
  thread_id: "thread-1",
  graph_version_ref: "workflow-1@1",
  deployment_ref: "demo-data-quality-repair-loop",
  status: "running",
  parent_run_id: null,
  current_step: "worker",
  approval_paused_state: null,
  failure_state: null,
  terminal_output: null,
};

let container: HTMLDivElement;
let root: Root;

function button(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
}

async function waitFor(assertion: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      failure = error;
      await act(async () => {
        await new Promise((resolve) => window.setTimeout(resolve, 0));
      });
    }
  }
  throw failure;
}

async function setNativeValue(
  element: HTMLSelectElement | HTMLTextAreaElement,
  value: string,
) {
  const prototype = element instanceof HTMLSelectElement
    ? HTMLSelectElement.prototype
    : HTMLTextAreaElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event(
      element instanceof HTMLSelectElement ? "change" : "input",
      { bubbles: true },
    ));
  });
}

async function mountPage() {
  await act(async () => root.render(<RunsPage />));
  await waitFor(() => expect(button("Cancel")).toBeTruthy());
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  api.listRuns.mockResolvedValue({ runs: [runningRun] });
  api.getRun.mockResolvedValue(runningRun);
  api.getChildRuns.mockResolvedValue([
    {
      run_id: "child-1",
      status: "succeeded",
      deployment_ref: "deterministic-child",
      graph_version_ref: "child@1",
      thread_id: "thread-child-1",
      parent_run_id: "run-1",
    },
    {
      run_id: "child-2",
      status: "succeeded",
      deployment_ref: "deterministic-child",
      graph_version_ref: "child@1",
      thread_id: "thread-child-2",
      parent_run_id: "run-1",
    },
  ]);
  api.getRunTimeline.mockResolvedValue({ entries: [] });
  api.getRunEvidence.mockResolvedValue({
    run: runningRun,
    summary: {
      audit_count: 0,
      approval_count: 0,
      tool_call_count: 0,
      memory_interaction_count: 0,
      priced_call_count: 0,
      cost_event_count: 0,
      total_cost_usd: 0,
      cost_identity_state: "not_applicable_no_priced_call",
      reconciliation_state: "reconciled_zero_activity",
    },
    audits: [],
    approvals: [],
    policy_events: [],
  });
  api.getHealth.mockResolvedValue({ campaign_id: "evaluation-studio-v1" });
  api.getInputContract.mockResolvedValue({
    name: "workflow3.action",
    version: 1,
    json_schema: {
      type: "object",
      required: ["ticket", "status"],
      properties: {
        ticket: { type: "string", pattern: "^synthetic-" },
        status: { type: "string", const: "remediated" },
      },
    },
  });
  api.cancelRun.mockResolvedValue({ ...runningRun, status: "cancelled" });
  api.resolveAmbiguousOperation.mockResolvedValue({
    operation_key: "operation-ambiguous-1",
    state: "COMPLETED",
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("run cancellation", () => {
  it("leaves the run visible and makes no API call when confirmation is declined", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountPage();

    await act(async () => button("Cancel")?.click());

    expect(confirm).toHaveBeenCalledWith("Cancel run run-1?");
    expect(api.cancelRun).not.toHaveBeenCalled();
    expect(container.textContent).toContain("run-1");
    expect(container.textContent).toContain("demo-data-quality-repair-loop");
    expect(button("Cancel")).toBeTruthy();
  });

  it("keeps a cancellation API failure visible", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    api.cancelRun.mockRejectedValue(new Error("control plane offline"));
    await mountPage();

    await act(async () => button("Cancel")?.click());
    await waitFor(() => {
      const alert = container.querySelector('[role="alert"]');
      expect(alert).toBeTruthy();
      expect(alert?.textContent).toContain("Cancel failed: Error: control plane offline");
    });

    expect(button("Cancel")).toBeTruthy();
  });
});

describe("run evidence", () => {
  it("uses the reconciled composed evidence total and preserves sub-cent precision", async () => {
    api.getRunTimeline.mockResolvedValue({
      entries: [
        {
          node_id: "batch",
          status: "completed",
          cost_usd: 0,
        },
      ],
    });
    api.getRunEvidence.mockResolvedValue({
      run: runningRun,
      summary: {
        audit_count: 34,
        approval_count: 0,
        tool_call_count: 0,
        memory_interaction_count: 0,
        priced_call_count: 8,
        cost_event_count: 8,
        total_cost_usd: 0.0009606,
        cost_identity_state: "correlated",
        reconciliation_state: "reconciled",
      },
      audits: [],
      approvals: [],
      policy_events: [],
    });

    await mountPage();
    await waitFor(() => {
      expect(container.textContent).toContain("attributed cost");
      expect(container.textContent).toContain("$0.000961");
      expect(container.textContent).toContain("8 priced calls");
    });
  });

  function ambiguousEvidence() {
    return {
      run: runningRun,
      summary: {
        audit_count: 1,
        approval_count: 0,
        tool_call_count: 1,
        memory_interaction_count: 0,
        priced_call_count: 0,
        cost_event_count: 0,
        total_cost_usd: 0,
        cost_identity_state: "not_applicable_no_priced_call",
        reconciliation_state: "needs_reconciliation",
      },
      audits: [
        {
          audit_id: "audit-operation-1",
          attempt: 1,
          deployment_ref: runningRun.deployment_ref,
          digest_version: 1,
          erased: false,
          graph_version_ref: runningRun.graph_version_ref,
          node_id: "apply-change",
          node_version: 1,
          run_id: runningRun.run_id,
          status: "failed",
          tenant_id: "tenant-1",
          tool_calls: [
            {
              alias: "ticketing",
              tool_ref: "eu://ticketing",
              operation_key: "operation-ambiguous-1",
              operation_state: "AMBIGUOUS",
              operation_reconciliation_required: true,
            },
          ],
        },
      ],
      approvals: [],
      policy_events: [],
    };
  }

  it("does not offer resolution when evidence has no ambiguous tool operation", async () => {
    await mountPage();
    await waitFor(() => expect(api.getRunEvidence).toHaveBeenCalled());
    expect(container.querySelector('[data-evidence-id^="runs.evidence.operation-resolution."]'))
      .toBeNull();
  });

  it("offers resolution for an ambiguous tool operation without promising run continuation", async () => {
    api.getRunEvidence.mockResolvedValue(ambiguousEvidence());
    await mountPage();
    await waitFor(() => {
      expect(container.querySelector(
        '[data-evidence-id="runs.evidence.operation-resolution.operation-ambiguous-1"]',
      )).toBeTruthy();
    });
    expect(container.textContent).toContain("does not resume or replay the run");
  });

  it("requires a reason and resolves with an optional JSON receipt before refreshing evidence", async () => {
    api.getRunEvidence
      .mockResolvedValueOnce(ambiguousEvidence())
      .mockResolvedValueOnce({
        ...ambiguousEvidence(),
        audits: [],
        summary: { ...ambiguousEvidence().summary, tool_call_count: 0 },
      });
    await mountPage();
    await waitFor(() => expect(button("Record resolution")).toBeTruthy());

    const submit = button("Record resolution")!;
    const resolution = container.querySelector<HTMLSelectElement>(
      '[data-evidence-id="runs.evidence.operation-resolution.operation-ambiguous-1.outcome"]',
    )!;
    const reason = container.querySelector<HTMLTextAreaElement>(
      '[data-evidence-id="runs.evidence.operation-resolution.operation-ambiguous-1.reason"]',
    )!;
    const receipt = container.querySelector<HTMLTextAreaElement>(
      '[data-evidence-id="runs.evidence.operation-resolution.operation-ambiguous-1.receipt"]',
    )!;
    expect(submit.disabled).toBe(true);

    await setNativeValue(resolution, "failed");
    await setNativeValue(reason, "Provider confirms the write did not commit.");
    await setNativeValue(receipt, '{"provider_status":"not_found"}');
    expect(submit.disabled).toBe(false);

    await act(async () => submit.click());
    await waitFor(() => expect(api.resolveAmbiguousOperation).toHaveBeenCalledWith(
      runningRun.deployment_ref,
      "operation-ambiguous-1",
      {
        resolution: "failed",
        reason: "Provider confirms the write did not commit.",
        receipt: { provider_status: "not_found" },
      },
    ));
    await waitFor(() => expect(api.getRunEvidence).toHaveBeenCalledTimes(2));
  });

  it.each([
    [403, "The active role does not have permission to resolve ambiguous operations."],
    [409, "This operation is no longer ambiguous. Refresh the evidence before acting again."],
    [503, "Resolution is temporarily unavailable because its signed audit or operation store is unavailable."],
  ])("explains operation resolution HTTP %i failures", async (status, expected) => {
    const { ApiError } = await import("@/app/lib/api");
    api.getRunEvidence.mockResolvedValue(ambiguousEvidence());
    api.resolveAmbiguousOperation.mockRejectedValue(new ApiError(status, "backend detail"));
    await mountPage();
    await waitFor(() => expect(button("Record resolution")).toBeTruthy());

    const reason = container.querySelector<HTMLTextAreaElement>(
      '[data-evidence-id="runs.evidence.operation-resolution.operation-ambiguous-1.reason"]',
    )!;
    await setNativeValue(reason, "Authoritative operator determination.");
    await act(async () => button("Record resolution")?.click());

    await waitFor(() => expect(container.querySelector(
      '[data-evidence-id="runs.evidence.operation-resolution.operation-ambiguous-1.error"]',
    )?.textContent).toContain(expected));
  });

  it("replaces a redaction-only timeline error with safe persisted failure metadata", async () => {
    api.getRunTimeline.mockResolvedValue({
      entries: [
        {
          audit_id: "audit-failed-1",
          attempt: 1,
          deployment_ref: runningRun.deployment_ref,
          digest_version: 1,
          erased: false,
          graph_version_ref: runningRun.graph_version_ref,
          node_id: "deterministic-delay",
          node_version: 1,
          run_id: runningRun.run_id,
          status: "failed",
          tenant_id: "tenant-1",
          error: "***REDACTED***",
          execution_metadata: { reason_code: "executable_unit_execution_error" },
        },
      ],
    });

    await mountPage();
    await waitFor(() => {
      expect(container.textContent).toContain("failed · executable unit execution error");
    });
    expect(container.textContent).not.toContain("***REDACTED***");
  });

  it("presents compacted context as an operator-readable runtime checkpoint", async () => {
    api.getRunTimeline.mockResolvedValue({
      entries: [
        {
          audit_id: "audit-context-1",
          attempt: 1,
          deployment_ref: runningRun.deployment_ref,
          digest_version: 1,
          erased: false,
          graph_version_ref: runningRun.graph_version_ref,
          node_id: "research",
          node_version: 1,
          run_id: runningRun.run_id,
          status: "completed",
          tenant_id: "tenant-1",
          execution_metadata: {
            context_compaction_applied: true,
            context_compaction_strategy: "truncation",
            context_tokens_before: 48,
            context_tokens_after: 19,
            context_messages_before: 7,
            context_messages_after: 3,
            thread_state_checkpointed: true,
            compacted_thread_state_saved: true,
          },
        },
      ],
    });

    await mountPage();
    await waitFor(() => {
      const context = container.querySelector('[data-evidence-id="runs.context-window"]');
      expect(context?.textContent).toContain("Context management");
      expect(context?.textContent).toContain("truncation");
      expect(context?.textContent).toContain("48 → 19 tokens");
      expect(context?.textContent).toContain("7 → 3 messages");
      expect(context?.textContent).toContain("1 compaction");
      expect(context?.textContent).toContain("state saved to thread thread-1");
    });
  });

  it("shows payload-free parent and child lineage links", async () => {
    api.getRun.mockResolvedValue({ ...runningRun, parent_run_id: "parent-0" });
    await mountPage();

    await waitFor(() => {
      expect(api.getChildRuns).toHaveBeenCalledWith("run-1");
      expect(container.querySelector('[data-evidence-id="runs.lineage.parent"]')?.textContent)
        .toContain("parent-0");
      expect(container.querySelector('[data-evidence-id="runs.lineage.children"]')?.textContent)
        .toContain("child-1");
      expect(container.querySelector('[data-evidence-id="runs.lineage.children"]')?.textContent)
        .toContain("child-2");
    });
    expect(container.textContent).not.toContain("terminal_output");
  });

  it("shows a minted thread identity once when it is identical to the run identity", async () => {
    api.getRun.mockResolvedValue({
      ...runningRun,
      run_id: "run-1",
      thread_id: "run-1",
    });

    await mountPage();

    await waitFor(() => {
      const details = container.querySelector('[aria-label="Run details"]');
      const exactIdentityLabels = Array.from(details?.querySelectorAll("span") ?? [])
        .filter((element) => element.textContent === "run-1");
      expect(exactIdentityLabels).toHaveLength(1);
    });
  });

  it("builds the invoke cURL from the selected deployment input contract", async () => {
    await mountPage();
    await waitFor(() => expect(api.getInputContract).toHaveBeenCalledWith(runningRun.deployment_ref));

    expect(container.textContent).toContain(
      '\"input_payload\": {\"ticket\":\"synthetic-example\",\"status\":\"remediated\"}',
    );
    expect(container.textContent).not.toContain('\"question\"');
  });

  it("shows a review summary and keeps metadata-only raw JSON collapsed", async () => {
    api.getRunEvidence.mockResolvedValue({
      run: runningRun,
      summary: {
        audit_count: 2,
        approval_count: 1,
        tool_call_count: 0,
        memory_interaction_count: 0,
        priced_call_count: 0,
        cost_event_count: 0,
        total_cost_usd: 0,
        cost_identity_state: "not_applicable_no_priced_call",
        reconciliation_state: "reconciled_zero_activity",
      },
      audits: [
        {
          audit_id: "audit-1",
          attempt: 1,
          deployment_ref: "demo-loop",
          digest_version: 1,
          erased: false,
          graph_version_ref: "workflow-1@1",
          node_id: "assess",
          node_version: 1,
          run_id: "run-1",
          status: "succeeded",
          tenant_id: "tenant-1",
          record_digest: "0123456789abcdef",
          record_signature: "abcdef0123456789",
          input_snapshot: { secret: "***REDACTED***" },
        },
      ],
      approvals: [],
      policy_events: [],
    });

    await mountPage();
    await waitFor(() => {
      expect(container.textContent).toContain("2 audit records");
      expect(container.textContent).toContain("1 approval");
      expect(container.textContent).toContain("Payload values are intentionally withheld");
      expect(container.textContent).toContain("No priced calls");
      expect(container.textContent).toContain("Cost identity not applicable");
      expect(container.textContent).toContain("$0 reconciled");
    });

    expect(button("Show raw evidence")).toBeTruthy();
    expect(container.textContent).not.toContain("***REDACTED***");

    await act(async () => button("Show raw evidence")?.click());
    expect(container.textContent).toContain("***REDACTED***");
    expect(button("Hide raw evidence")).toBeTruthy();
  });
});
