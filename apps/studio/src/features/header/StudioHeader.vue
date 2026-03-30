<script setup lang="ts">
import { computed } from "vue";
import { ChevronDown, CloudUpload, Play, Save } from "lucide-vue-next";

import { useStudioShellStore } from "@/stores/studioShell";

const shellStore = useStudioShellStore();

const workflowTitle = computed(() => shellStore.selectedWorkflowName || "Start your first workflow");

const saveStateLabel = computed(() => {
  switch (shellStore.saveStatus) {
    case "dirty":
      return "Unsaved changes";
    case "saving":
      return "Saving draft";
    case "saved":
      return "Saved just now";
    case "error":
      return "Save needs attention";
    default:
      return "Draft ready";
  }
});
</script>

<template>
  <header class="studio-header">
    <div class="studio-header__title">
      <p class="studio-label">Workflow</p>
      <div class="studio-header__headline">
        <h1 class="studio-display">{{ workflowTitle }}</h1>
        <span class="studio-header__save-state">
          <Save :size="14" aria-hidden="true" />
          {{ saveStateLabel }}
        </span>
      </div>
    </div>
    <div class="studio-header__actions">
      <div class="studio-header__environment" role="button" tabindex="0" aria-label="Manage environments">
        <span class="studio-header__environment-copy">
          <span class="studio-label">Environment</span>
          <strong>{{ shellStore.currentEnvironment }}</strong>
        </span>
        <ChevronDown :size="16" aria-hidden="true" />
      </div>
      <button class="studio-header__ghost-action" type="button">Manage environments</button>
      <button class="studio-header__secondary-action" type="button">
        <Play :size="16" aria-hidden="true" />
        Run Draft
      </button>
      <button class="studio-header__primary-action" type="button">
        <CloudUpload :size="16" aria-hidden="true" />
        Publish
      </button>
    </div>
  </header>
</template>
