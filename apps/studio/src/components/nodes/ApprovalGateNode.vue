<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeProps } from '@vue-flow/core'
import BaseNode from './BaseNode.vue'

defineProps<NodeProps>()
</script>

<template>
  <BaseNode :selected="selected" :label="data.label" meta-label="APPROVAL GATE">
    <template #icon>
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M10 2L3 6v5c0 4 3 6.5 7 8 4-1.5 7-4 7-8V6l-7-4z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>
        <path d="M7.5 10l2 2 3.5-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </template>
    <Handle type="target" :position="Position.Left" id="input-control" class="studio-handle" />
    <Handle type="source" :position="Position.Right" id="output-control" class="studio-handle" />
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
