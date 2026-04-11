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
    meta-label="CONDITION"
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
          d="M10 2l6 8-6 8-6-8 6-8z"
          stroke="currentColor"
          stroke-width="1.3"
          stroke-linejoin="round"
        />
        <path
          d="M8 10h4"
          stroke="currentColor"
          stroke-width="1.3"
          stroke-linecap="round"
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
      id="output-true"
      type="source"
      :position="Position.Right"
      class="studio-handle studio-handle--true"
    />
    <Handle
      id="output-false"
      type="source"
      :position="Position.Right"
      class="studio-handle studio-handle--false"
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
.studio-handle--true {
  top: 30%;
}
.studio-handle--false {
  top: 70%;
}
</style>
