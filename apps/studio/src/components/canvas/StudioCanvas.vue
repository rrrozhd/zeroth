<script setup lang="ts">
import { VueFlow, ConnectionMode } from '@vue-flow/core'
import type { Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { useCanvasStore } from '../../stores/canvas'
import { useUiStore } from '../../stores/ui'
import { isValidConnection } from '../../composables/usePortValidation'
import AgentNode from '../nodes/AgentNode.vue'
import ExecutionUnitNode from '../nodes/ExecutionUnitNode.vue'
import ApprovalGateNode from '../nodes/ApprovalGateNode.vue'
import MemoryResourceNode from '../nodes/MemoryResourceNode.vue'
import ConditionBranchNode from '../nodes/ConditionBranchNode.vue'
import StartNode from '../nodes/StartNode.vue'
import EndNode from '../nodes/EndNode.vue'
import DataMappingNode from '../nodes/DataMappingNode.vue'
import CanvasControls from './CanvasControls.vue'
import CanvasMinimap from './CanvasMinimap.vue'

const canvasStore = useCanvasStore()
const uiStore = useUiStore()

const nodeTypes = {
  agent: AgentNode,
  executionUnit: ExecutionUnitNode,
  approvalGate: ApprovalGateNode,
  memoryResource: MemoryResourceNode,
  conditionBranch: ConditionBranchNode,
  start: StartNode,
  end: EndNode,
  dataMapping: DataMappingNode,
}

function onConnect(connection: Connection) {
  canvasStore.addEdge(connection)
}

function onNodeClick({ node }: { event: MouseEvent | TouchEvent; node: { id: string } }) {
  uiStore.selectedNodeId = node.id
}

function onPaneClick() {
  uiStore.selectedNodeId = null
}
</script>

<template>
  <div class="studio-canvas">
    <VueFlow
      v-model:nodes="canvasStore.nodes"
      v-model:edges="canvasStore.edges"
      :node-types="nodeTypes"
      :connection-mode="ConnectionMode.Strict"
      :is-valid-connection="isValidConnection"
      :default-edge-options="{ type: 'default', style: { stroke: 'rgba(79, 180, 220, 0.6)', strokeWidth: 2 } }"
      :min-zoom="0.25"
      :max-zoom="4"
      :snap-to-grid="true"
      :snap-grid="[24, 24]"
      fit-view-on-init
      @connect="onConnect"
      @node-click="onNodeClick"
      @pane-click="onPaneClick"
    >
      <Background :gap="24" :size="1" pattern-color="rgba(150, 180, 200, 0.18)" />
      <CanvasMinimap />
      <CanvasControls />
    </VueFlow>
  </div>
</template>

<style scoped>
.studio-canvas {
  position: relative;
  width: 100%;
  height: 100%;
}
</style>
