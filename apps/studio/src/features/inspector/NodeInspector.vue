<script setup lang="ts">
import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { Clock3, ShieldCheck } from "lucide-vue-next";

import ContractPanel from "@/features/inspector/ContractPanel.vue";
import ValidationSummary from "@/features/validation/ValidationSummary.vue";
import { createValidationApiClient } from "@/lib/api/validation";
import { useStudioShellStore, type StudioRouteMode } from "@/stores/studioShell";

defineProps<{
  mode: StudioRouteMode;
}>();

const shellStore = useStudioShellStore();
const validationClient = createValidationApiClient();

const selectedNode = computed(() => {
  const nodes = shellStore.workflowGraph?.nodes ?? [];
  return (
    nodes.find((node) => String((node as Record<string, unknown>).id ?? "") === shellStore.selectedNodeId) ?? null
  ) as Record<string, unknown> | null;
});

const selectedNodeTitle = computed(() => {
  if (!shellStore.selectedNodeId) {
    return "No node selected";
  }

  const label = selectedNode.value?.name ?? selectedNode.value?.title ?? shellStore.selectedNodeId;
  return String(label);
});

const activityRows = computed(() => {
  if (!selectedNode.value) {
    return [
      "Select a node to inspect contract bindings and local validation issues.",
      "Runtime detail stays compact here until Executions mode expands it.",
    ];
  }

  return [
    `Lease state is ${shellStore.hasActiveLease ? "active" : "idle"} for this workflow draft.`,
    shellStore.conflictState === "conflict"
      ? "Draft conflict detected. Refresh the workflow before retrying a save."
      : "Draft writes remain scoped to the current revision token.",
    `Node type: ${String(selectedNode.value.type ?? "unknown")}.`,
  ];
});

const validationQuery = useQuery({
  queryKey: computed(() => ["studio", "validation", shellStore.selectedWorkflowId]),
  enabled: computed(() => Boolean(shellStore.selectedWorkflowId)),
  queryFn: async () => {
    if (!shellStore.selectedWorkflowId) {
      return null;
    }

    try {
      return await validationClient.validateWorkflow(shellStore.selectedWorkflowId);
    } catch {
      return null;
    }
  },
});

const inputContractRef = computed(() => {
  if (!selectedNode.value) {
    return null;
  }
  return String(selectedNode.value.input_contract_ref ?? "") || null;
});

const outputContractRef = computed(() => {
  if (!selectedNode.value) {
    return null;
  }
  return String(selectedNode.value.output_contract_ref ?? "") || null;
});
</script>

<template>
  <aside class="node-inspector">
    <div class="node-inspector__section">
      <p class="studio-label">Inspector</p>
      <h2 class="studio-heading">{{ selectedNodeTitle }}</h2>
      <p class="studio-body">
        Node-local authoring stays focused here while deeper runtime records stay behind explicit navigation.
      </p>
    </div>

    <div class="node-inspector__section">
      <ContractPanel :input-contract-ref="inputContractRef" :output-contract-ref="outputContractRef" />
    </div>

    <div class="node-inspector__section">
      <ValidationSummary :report="validationQuery.data.value ?? null" :node-id="shellStore.selectedNodeId" />
    </div>

    <div class="node-inspector__section">
      <div class="node-inspector__eyebrow">
        <Clock3 :size="16" aria-hidden="true" />
        <span>Recent activity</span>
      </div>
      <ul class="node-inspector__activity-list">
        <li v-for="item in activityRows" :key="item">{{ item }}</li>
      </ul>
    </div>

    <div class="node-inspector__section">
      <div class="node-inspector__eyebrow">
        <ShieldCheck :size="16" aria-hidden="true" />
        <span>Context</span>
      </div>
      <p class="studio-body">
        Validation, approvals, and evidence stay ambient until you move into Executions or Tests.
      </p>
    </div>
  </aside>
</template>
