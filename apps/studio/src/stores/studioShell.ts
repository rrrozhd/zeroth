import { defineStore } from "pinia";
import { ref } from "vue";

export type StudioRouteMode = "editor" | "executions" | "tests";
export type StudioSaveStatus = "idle" | "dirty" | "saving" | "saved" | "error";

export const useStudioShellStore = defineStore("studioShell", () => {
  const selectedWorkflowId = ref<string | null>(null);
  const selectedNodeId = ref<string | null>(null);
  const currentRouteMode = ref<StudioRouteMode>("editor");
  const saveStatus = ref<StudioSaveStatus>("idle");
  const leaseToken = ref<string | null>(null);

  function setSelectedWorkflowId(workflowId: string | null): void {
    selectedWorkflowId.value = workflowId;
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

  function setLeaseToken(token: string | null): void {
    leaseToken.value = token;
  }

  return {
    selectedWorkflowId,
    selectedNodeId,
    currentRouteMode,
    saveStatus,
    leaseToken,
    setSelectedWorkflowId,
    setSelectedNodeId,
    setCurrentRouteMode,
    setSaveStatus,
    setLeaseToken,
  };
});
