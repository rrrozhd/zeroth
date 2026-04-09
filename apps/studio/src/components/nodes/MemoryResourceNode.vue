<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeProps } from '@vue-flow/core'
import BaseNode from './BaseNode.vue'

defineProps<NodeProps>()
</script>

<template>
  <BaseNode :selected="selected" :label="data.label" meta-label="MEMORY">
    <template #icon>
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <ellipse cx="10" cy="5" rx="6" ry="2.5" stroke="currentColor" stroke-width="1.3"/>
        <path d="M4 5v4c0 1.38 2.69 2.5 6 2.5s6-1.12 6-2.5V5" stroke="currentColor" stroke-width="1.3"/>
        <path d="M4 9v4c0 1.38 2.69 2.5 6 2.5s6-1.12 6-2.5V9" stroke="currentColor" stroke-width="1.3"/>
      </svg>
    </template>
    <Handle type="target" :position="Position.Top" id="input-data" class="studio-handle" />
    <Handle type="source" :position="Position.Right" id="output-data" class="studio-handle" />
  </BaseNode>
</template>

<style scoped>
.studio-handle {
  width: 10px;
  height: 10px;
  border: 1.5px solid rgba(79, 180, 220, 0.7);
  background: rgba(255, 255, 255, 0.9);
  transition: transform 150ms ease, border-color 150ms ease;
}
.studio-handle:hover {
  transform: scale(1.5);
  border-color: rgba(79, 205, 255, 1);
}
</style>
