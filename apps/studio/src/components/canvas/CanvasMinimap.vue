<script setup lang="ts">
import { MiniMap } from '@vue-flow/minimap'
</script>

<template>
  <div class="canvas-minimap">
    <MiniMap
      :width="110"
      :height="72"
      :node-color="'rgba(79, 180, 220, 0.4)'"
      :mask-color="'rgba(247, 251, 255, 0.7)'"
    />
  </div>
</template>

<style scoped>
.canvas-minimap {
  position: absolute;
  bottom: 72px;
  left: 16px;
  z-index: 10;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid rgba(118, 182, 205, 0.3);
  box-shadow: 0 4px 12px rgba(117, 160, 189, 0.08);
}

.canvas-minimap :deep(.vue-flow__minimap) {
  background: rgba(255, 255, 255, 0.85);
}

.canvas-minimap :deep(.vue-flow__minimap-mask) {
  fill: rgba(247, 251, 255, 0.7);
}

.canvas-minimap :deep(.vue-flow__minimap svg) {
  width: 110px;
  height: 72px;
}
</style>
