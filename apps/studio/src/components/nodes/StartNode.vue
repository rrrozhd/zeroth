<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeProps } from '@vue-flow/core'
import BaseNode from './BaseNode.vue'

defineProps<NodeProps>()
</script>

<template>
  <BaseNode :selected="selected" :label="data.label" meta-label="START">
    <template #icon>
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
        <path d="M13 3l-1.5 3H8L6.5 3M7 6v5.5M13 6v5.5M5 11h10l2 6H3l2-6z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M10 2v2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>
    </template>
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
