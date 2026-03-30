<script setup lang="ts">
import { computed } from "vue";
import { Clock3, FileCode2, ShieldCheck } from "lucide-vue-next";

import { useStudioShellStore, type StudioRouteMode } from "@/stores/studioShell";

defineProps<{
  mode: StudioRouteMode;
}>();

const shellStore = useStudioShellStore();

const selectedNodeTitle = computed(() => {
  if (!shellStore.selectedNodeId) {
    return "No node selected";
  }

  return shellStore.selectedNodeId
    .split("-")
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(" ");
});

const activityRows = [
  "Contract check passed for the latest draft snapshot.",
  "Last execution surfaced a compact approval trace.",
  "Mapped output remains aligned to the downstream schema.",
];
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
      <div class="node-inspector__eyebrow">
        <FileCode2 :size="16" aria-hidden="true" />
        <span>Contract summary</span>
      </div>
      <div class="node-inspector__card">
        <strong>Input</strong>
        <p class="studio-body">Typed request payload with governance metadata placeholders.</p>
      </div>
      <div class="node-inspector__card">
        <strong>Output</strong>
        <p class="studio-body">Structured response and evidence handles return through this node boundary.</p>
      </div>
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
        <span>{{ mode === "editor" ? "Context" : "Upcoming deeper view" }}</span>
      </div>
      <p class="studio-body">
        {{ mode === "editor"
          ? "Validation, approvals, and evidence stay ambient until you move into Executions or Tests."
          : "This shared inspector remains narrow while the center pane changes mode." }}
      </p>
    </div>
  </aside>
</template>
