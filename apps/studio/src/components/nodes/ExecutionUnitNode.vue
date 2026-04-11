<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import type { NodeProps } from '@vue-flow/core'
import BaseNode from './BaseNode.vue'

defineProps<NodeProps>()
</script>

<template>
  <BaseNode
    :selected="selected"
    :label="data.label"
    meta-label="EXECUTION UNIT"
    :node-id="id"
  >
    <template #icon>
      <svg
        width="20"
        height="20"
        viewBox="0 0 20 20"
        fill="none"
      >
        <path
          d="M4 10h8M12 10l-3-3M12 10l-3 3"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <circle
          cx="15"
          cy="10"
          r="2"
          stroke="currentColor"
          stroke-width="1.5"
        />
      </svg>
    </template>
    <Handle
      id="input-data"
      type="target"
      :position="Position.Left"
      class="studio-handle"
    />
    <Handle
      id="output-data"
      type="source"
      :position="Position.Right"
      class="studio-handle"
    />
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
