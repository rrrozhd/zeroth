import { defineStore } from "pinia";
import { ref } from "vue";

export type StudioRouteMode = "editor" | "executions" | "tests";
export type StudioSaveStatus = "idle" | "dirty" | "saving" | "saved" | "error";

export const useStudioShellStore = defineStore("studioShell", () => {
  const selectedWorkflowId = ref<string | null>(null);
  const selectedWorkflowName = ref<string | null>(null);
  const selectedNodeId = ref<string | null>(null);
  const currentRouteMode = ref<StudioRouteMode>("editor");
  const saveStatus = ref<StudioSaveStatus>("saved");
  const leaseToken = ref<string | null>(null);
  const currentEnvironment = ref("Draft");

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

  function setLeaseToken(token: string | null): void {
    leaseToken.value = token;
  }

  function setCurrentEnvironment(environment: string): void {
    currentEnvironment.value = environment;
  }

  return {
    selectedWorkflowId,
    selectedWorkflowName,
    selectedNodeId,
    currentRouteMode,
    saveStatus,
    leaseToken,
    currentEnvironment,
    setSelectedWorkflowId,
    setSelectedWorkflowName,
    setSelectedNodeId,
    setCurrentRouteMode,
    setSaveStatus,
    setLeaseToken,
    setCurrentEnvironment,
  };
});
