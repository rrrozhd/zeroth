import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

import type { WorkflowGraph } from "@/lib/api/studio";
import { createValidationApiClient } from "@/lib/api/validation";
import {
  resetStudioShellApiForTests,
  setStudioShellApiForTests,
  useStudioShellStore,
} from "@/stores/studioShell";

function flushPromises(): Promise<void> {
  return new Promise((resolve) => {
    queueMicrotask(() => resolve());
  });
}

describe("studioShell lease orchestration", () => {
  const graph: WorkflowGraph = {
    graph_id: "graph-1",
    version: 3,
    entry_step: "node-a",
    nodes: [{ id: "node-a", type: "agent", name: "Node A" }],
    edges: [],
  };

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-03-30T10:00:00.000Z"));
    setActivePinia(createPinia());
  });

  afterEach(() => {
    resetStudioShellApiForTests();
    vi.useRealTimers();
  });

  it("does not save drafts when the lease is missing, expired, or cleared", async () => {
    const updateWorkflowDraft = vi.fn();

    setStudioShellApiForTests({
      acquireWorkflowLease: vi.fn(),
      renewWorkflowLease: vi.fn(),
      releaseWorkflowLease: vi.fn(),
      updateWorkflowDraft,
    });

    const store = useStudioShellStore();

    store.setSelectedWorkflowId("workflow-a");
    store.setRevisionToken("revision-a");

    await store.saveWorkflowDraft(graph);
    expect(updateWorkflowDraft).not.toHaveBeenCalled();

    store.leaseToken = "lease-a";
    store.leaseExpiresAt = "2026-03-30T09:59:00.000Z";

    await store.saveWorkflowDraft(graph);
    expect(updateWorkflowDraft).not.toHaveBeenCalled();

    store.leaseExpiresAt = "2026-03-30T10:05:00.000Z";
    await store.releaseWorkflowLease();
    await store.saveWorkflowDraft(graph);

    expect(updateWorkflowDraft).not.toHaveBeenCalled();
  });

  it("acquires a lease, renews it before expiry, and clears it on workflow switch and route leave", async () => {
    const acquireWorkflowLease = vi
      .fn()
      .mockResolvedValueOnce({
        workflow_id: "workflow-a",
        workspace_id: "workspace-1",
        lease_token: "lease-a",
        expires_at: "2026-03-30T10:01:00.000Z",
      })
      .mockResolvedValueOnce({
        workflow_id: "workflow-b",
        workspace_id: "workspace-1",
        lease_token: "lease-b",
        expires_at: "2026-03-30T10:02:00.000Z",
      });
    const renewWorkflowLease = vi.fn().mockResolvedValue({
      workflow_id: "workflow-a",
      workspace_id: "workspace-1",
      lease_token: "lease-a-renewed",
      expires_at: "2026-03-30T10:02:00.000Z",
    });
    const releaseWorkflowLease = vi.fn().mockResolvedValue(undefined);

    setStudioShellApiForTests({
      acquireWorkflowLease,
      renewWorkflowLease,
      releaseWorkflowLease,
      updateWorkflowDraft: vi.fn(),
    });

    const store = useStudioShellStore();

    await store.openWorkflow({
      workflowId: "workflow-a",
      workflowName: "Workflow A",
      revisionToken: "revision-a",
    });

    expect(store.leaseToken).toBe("lease-a");
    expect(acquireWorkflowLease).toHaveBeenCalledWith("workflow-a");

    vi.advanceTimersByTime(55_000);
    await flushPromises();

    expect(renewWorkflowLease).toHaveBeenCalledWith("workflow-a", "lease-a");
    expect(store.leaseToken).toBe("lease-a-renewed");

    await store.openWorkflow({
      workflowId: "workflow-b",
      workflowName: "Workflow B",
      revisionToken: "revision-b",
    });

    expect(releaseWorkflowLease).toHaveBeenCalledWith("workflow-a", "lease-a-renewed");
    expect(store.leaseToken).toBe("lease-b");

    window.dispatchEvent(new Event("beforeunload"));
    await flushPromises();

    expect(releaseWorkflowLease).toHaveBeenCalledWith("workflow-b", "lease-b");
    expect(store.leaseToken).toBeNull();
  });
});

describe("validation client", () => {
  it("looks up slash-safe contract refs through the encoded path route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          name: "contract://input",
          version: 7,
          json_schema: {
            title: "Input Contract",
            type: "object",
          },
        }),
        {
          status: 200,
          headers: {
            "content-type": "application/json",
          },
        },
      ),
    );

    const client = createValidationApiClient({
      baseUrl: "http://studio.test",
      fetch: fetchMock,
    });

    const contract = await client.lookupContract("contract://input");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://studio.test/studio/contracts/contract%3A%2F%2Finput",
      expect.objectContaining({
        method: "GET",
      }),
    );
    expect(contract.name).toBe("contract://input");
    expect(contract.json_schema.title).toBe("Input Contract");
  });
});
