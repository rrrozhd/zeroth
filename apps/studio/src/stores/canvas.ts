import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Connection, XYPosition } from '@vue-flow/core'
import { NODE_TYPE_REGISTRY } from '../types/nodes'

export interface CanvasNode {
  id: string
  type: string
  position: XYPosition
  data: { label: string; nodeType: string }
}

export interface CanvasEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string
  targetHandle?: string
}

export const useCanvasStore = defineStore('canvas', () => {
  const nodes = ref<CanvasNode[]>([])
  const edges = ref<CanvasEdge[]>([])

  let nodeCounter = 0

  function addNode(type: string, position: XYPosition) {
    const typeDef = NODE_TYPE_REGISTRY[type]
    if (!typeDef) return
    const id = `${type}-${++nodeCounter}-${Date.now()}`
    nodes.value.push({
      id,
      type,
      position,
      data: { label: typeDef.label, nodeType: type },
    })
  }

  function removeNode(id: string) {
    nodes.value = nodes.value.filter(n => n.id !== id)
    edges.value = edges.value.filter(e => e.source !== id && e.target !== id)
  }

  function addEdge(connection: Connection) {
    const id = `edge-${connection.source}-${connection.target}-${Date.now()}`
    edges.value.push({
      id,
      source: connection.source,
      target: connection.target,
      sourceHandle: connection.sourceHandle ?? undefined,
      targetHandle: connection.targetHandle ?? undefined,
    })
  }

  function removeEdge(id: string) {
    edges.value = edges.value.filter(e => e.id !== id)
  }

  function clearCanvas() {
    nodes.value = []
    edges.value = []
  }

  return { nodes, edges, addNode, removeNode, addEdge, removeEdge, clearCanvas }
})
