<script setup lang="ts">
import { computed, watch } from "vue";
import { useRoute } from "vue-router";

import StudioHeader from "@/features/header/StudioHeader.vue";
import NodeInspector from "@/features/inspector/NodeInspector.vue";
import ModeSwitch from "@/features/modes/ModeSwitch.vue";
import WorkflowCanvas from "@/features/canvas/WorkflowCanvas.vue";
import WorkflowRail from "@/features/workflows/WorkflowRail.vue";
import { useStudioShellStore } from "@/stores/studioShell";

const route = useRoute();
const shellStore = useStudioShellStore();

const activeMode = computed(() => {
  const routeName = route.name;
  if (routeName === "executions" || routeName === "tests") {
    return routeName;
  }
  return "editor";
});

watch(
  activeMode,
  (mode) => {
    shellStore.setCurrentRouteMode(mode);
  },
  { immediate: true },
);
</script>

<template>
  <div class="app-shell">
    <StudioHeader />
    <div class="app-shell__toolbar">
      <ModeSwitch />
    </div>
    <main class="app-shell__layout">
      <WorkflowRail />
      <WorkflowCanvas :mode="activeMode" />
      <NodeInspector :mode="activeMode" />
    </main>
  </div>
</template>
