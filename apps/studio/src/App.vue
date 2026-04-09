<script setup lang="ts">
import AppHeader from './components/shell/AppHeader.vue'
import WorkflowRail from './components/shell/WorkflowRail.vue'
import CanvasArea from './components/shell/CanvasArea.vue'
import { useUiStore } from './stores/ui'

const ui = useUiStore()
</script>

<template>
  <div class="shell">
    <AppHeader />
    <div class="shell-body">
      <WorkflowRail
        :collapsed="ui.railCollapsed"
        @toggle="ui.toggleRail()"
      />
      <CanvasArea />
    </div>
  </div>
</template>

<style scoped>
.shell {
  min-width: 900px;
  min-height: 100vh;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: linear-gradient(180deg, #f7fbff 0%, #eef7fb 100%);
}

.shell-body {
  display: flex;
  flex-direction: row;
  gap: 16px;
  flex: 1;
  min-height: 0;
}
</style>
