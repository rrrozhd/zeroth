import { computed, ref } from "vue";
import { defineStore } from "pinia";

import {
  StudioApiError,
  createStudioApiClient,
  type LeasePayload,
  type WorkflowDetail,
  type WorkflowGraph,
} from "@/lib/api/studio";

export type StudioRouteMode = "editor" | "executions" | "tests";
export type StudioSaveStatus = "idle" | "dirty" | "saving" | "saved" | "error";
export type StudioConflictState = "idle" | "conflict";

interface StudioShellApi {
  acquireWorkflowLease(workflowId: string): Promise<LeasePayload>;
  renewWorkflowLease(workflowId: string, leaseToken: string): Promise<LeasePayload>;
  releaseWorkflowLease(workflowId: string, leaseToken: string): Promise<void>;
  updateWorkflowDraft(
    workflowId: string,
    leaseToken: string,
    revisionToken: string,
    graph: WorkflowGraph,
  ): Promise<WorkflowDetail>;
}

let studioShellApi: StudioShellApi | null = null;

function getStudioShellApi(): StudioShellApi {
  studioShellApi ??= createStudioApiClient();
  return studioShellApi;
}

export function setStudioShellApiForTests(api: StudioShellApi): void {
  studioShellApi = api;
}

export function resetStudioShellApiForTests(): void {
  studioShellApi = null;
}

export const useStudioShellStore = defineStore("studioShell", () => {
  const selectedWorkflowId = ref<string | null>(null);
  const selectedWorkflowName = ref<string | null>(null);
  const selectedNodeId = ref<string | null>(null);
  const currentRouteMode = ref<StudioRouteMode>("editor");
  const saveStatus = ref<StudioSaveStatus>("saved");
  const leaseToken = ref<string | null>(null);
  const leaseExpiresAt = ref<string | null>(null);
  const revisionToken = ref<string | null>(null);
  const workflowGraph = ref<WorkflowGraph | null>(null);
  const conflictState = ref<StudioConflictState>("idle");
  const currentEnvironment = ref("Draft");

  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  let beforeUnloadBound = false;

  const hasActiveLease = computed(() => {
    if (!leaseToken.value || !leaseExpiresAt.value) {
      return false;
    }
    return new Date(leaseExpiresAt.value).getTime() > Date.now();
  });

  function clearHeartbeat(): void {
    if (heartbeatTimer) {
      clearTimeout(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function clearLeaseState(): void {
    clearHeartbeat();
    leaseToken.value = null;
    leaseExpiresAt.value = null;
  }

  function setSelectedWorkflowId(workflowId: string | null): void {
    selectedWorkflowId.value = workflowId;
  }

  function setSelectedWorkflowName(workflowName: string | null): void {
    selectedWorkflowName.value = workflowName;
  }

  function setSelectedNodeId(nodeId: string | null): void {
    selectedNodeId.value = nodeId;
  }

  function setCurrentRouteMode(mode: StudioRouteMode): void {
    currentRouteMode.value = mode;
  }

  function setSaveStatus(status: StudioSaveStatus): void {
    saveStatus.value = status;
  }

  function setRevisionToken(token: string | null): void {
    revisionToken.value = token;
  }

  function setCurrentEnvironment(environment: string): void {
    currentEnvironment.value = environment;
  }

  function setWorkflowDetail(detail: Pick<WorkflowDetail, "workflow_id" | "name" | "revision_token" | "graph">): void {
    selectedWorkflowId.value = detail.workflow_id;
    selectedWorkflowName.value = detail.name;
    revisionToken.value = detail.revision_token;
    workflowGraph.value = structuredClone(detail.graph);
  }

  function scheduleHeartbeat(payload: LeasePayload): void {
    clearHeartbeat();
    const expiresAt = new Date(payload.expires_at).getTime();
    const delay = Math.max(1_000, expiresAt - Date.now() - 5_000);

    heartbeatTimer = setTimeout(() => {
      void renewCurrentLease();
    }, delay);
  }

  async function renewCurrentLease(): Promise<void> {
    if (!selectedWorkflowId.value || !leaseToken.value) {
      clearLeaseState();
      return;
    }

    try {
      const payload = await getStudioShellApi().renewWorkflowLease(selectedWorkflowId.value, leaseToken.value);
      leaseToken.value = payload.lease_token;
      leaseExpiresAt.value = payload.expires_at;
      scheduleHeartbeat(payload);
    } catch {
      conflictState.value = "conflict";
      clearLeaseState();
    }
  }

  function bindBeforeUnload(): void {
    if (beforeUnloadBound || typeof window === "undefined") {
      return;
    }

    beforeUnloadBound = true;
    window.addEventListener("beforeunload", () => {
      const workflowId = selectedWorkflowId.value;
      const token = leaseToken.value;

      clearLeaseState();

      if (workflowId && token) {
        void getStudioShellApi().releaseWorkflowLease(workflowId, token);
      }
    });
  }

  async function releaseWorkflowLease(): Promise<void> {
    clearHeartbeat();
    const workflowId = selectedWorkflowId.value;
    const token = leaseToken.value;

    clearLeaseState();

    if (!workflowId || !token) {
      return;
    }

    try {
      await getStudioShellApi().releaseWorkflowLease(workflowId, token);
    } catch {
      conflictState.value = "conflict";
    }
  }

  async function openWorkflow(details: {
    workflowId: string;
    workflowName: string;
    revisionToken?: string | null;
  }): Promise<void> {
    const previousWorkflowId = selectedWorkflowId.value;
    const previousWorkflowName = selectedWorkflowName.value;
    const previousRevisionToken = revisionToken.value;
    const sameWorkflow =
      details.workflowId === previousWorkflowId &&
      details.workflowId !== null &&
      hasActiveLease.value;

    bindBeforeUnload();

    if (sameWorkflow) {
      selectedWorkflowName.value = details.workflowName;
      revisionToken.value = details.revisionToken ?? revisionToken.value;
      return;
    }

    if (leaseToken.value) {
      selectedWorkflowId.value = previousWorkflowId;
      selectedWorkflowName.value = previousWorkflowName;
      revisionToken.value = previousRevisionToken;
      await releaseWorkflowLease();
    }

    selectedWorkflowId.value = details.workflowId;
    selectedWorkflowName.value = details.workflowName;
    revisionToken.value = details.revisionToken ?? previousRevisionToken;
    selectedNodeId.value = null;
    conflictState.value = "idle";

    const payload = await getStudioShellApi().acquireWorkflowLease(details.workflowId);
    leaseToken.value = payload.lease_token;
    leaseExpiresAt.value = payload.expires_at;
    scheduleHeartbeat(payload);
  }

  async function saveWorkflowDraft(graph: WorkflowGraph): Promise<WorkflowDetail | null> {
    if (!selectedWorkflowId.value || !revisionToken.value || !hasActiveLease.value || !leaseToken.value) {
      return null;
    }

    saveStatus.value = "saving";
    conflictState.value = "idle";

    try {
      const detail = await getStudioShellApi().updateWorkflowDraft(
        selectedWorkflowId.value,
        leaseToken.value,
        revisionToken.value,
        graph,
      );
      revisionToken.value = detail.revision_token;
      workflowGraph.value = structuredClone(detail.graph);
      saveStatus.value = "saved";
      return detail;
    } catch (error) {
      saveStatus.value = "error";
      if (error instanceof StudioApiError && error.status === 409) {
        conflictState.value = "conflict";
      }
      return null;
    }
  }

  return {
    selectedWorkflowId,
    selectedWorkflowName,
    selectedNodeId,
    currentRouteMode,
    saveStatus,
    leaseToken,
    leaseExpiresAt,
    revisionToken,
    workflowGraph,
    conflictState,
    currentEnvironment,
    hasActiveLease,
    setSelectedWorkflowId,
    setSelectedWorkflowName,
    setSelectedNodeId,
    setCurrentRouteMode,
    setSaveStatus,
    setRevisionToken,
    setWorkflowDetail,
    setCurrentEnvironment,
    openWorkflow,
    renewCurrentLease,
    releaseWorkflowLease,
    saveWorkflowDraft,
  };
});
