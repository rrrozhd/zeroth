import { useCanvasStore, type CanvasNode, type CanvasEdge } from '../stores/canvas'
import { useWorkflowStore } from '../stores/workflow'
import * as api from '../api/workflows'

export function useWorkflowPersistence() {
  const canvasStore = useCanvasStore()
  const workflowStore = useWorkflowStore()

  async function saveWorkflow(getViewport: () => { x: number; y: number; zoom: number }) {
    const id = workflowStore.currentWorkflowId
    if (!id) return

    workflowStore.isSaving = true
    try {
      const viewport = getViewport()

      const nodes = canvasStore.nodes.map((n) => ({
        id: n.id,
        type: n.type ?? 'agent',
        position: n.position,
        data: n.data ?? {},
      }))

      const edges = canvasStore.edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        source_handle: e.sourceHandle ?? null,
        target_handle: e.targetHandle ?? null,
      }))

      const result = await api.updateWorkflow(id, {
        name: workflowStore.currentWorkflowName,
        nodes,
        edges,
        viewport: { x: viewport.x, y: viewport.y, zoom: viewport.zoom },
      })

      workflowStore.isDirty = false
      workflowStore.isSaving = false
      workflowStore.lastSavedAt = result.updated_at
      workflowStore.currentVersion = result.version
    } catch (_e) {
      workflowStore.isSaving = false
      workflowStore.error =
        'Could not save workflow. Check your connection and try again.'
    }
  }

  async function loadWorkflow(id: string) {
    const result = await workflowStore.loadWorkflow(id)
    if (!result) return null

    canvasStore.clearCanvas()
    // Restore nodes
    for (const node of result.nodes) {
      const canvasNode: CanvasNode = {
        id: node.id,
        type: node.type,
        position: node.position,
        data: node.data as Record<string, unknown>,
      }
      canvasStore.nodes.push(canvasNode)
    }
    // Restore edges
    for (const edge of result.edges) {
      const canvasEdge: CanvasEdge = {
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.source_handle ?? undefined,
        targetHandle: edge.target_handle ?? undefined,
      }
      canvasStore.edges.push(canvasEdge)
    }

    return result
  }

  return { saveWorkflow, loadWorkflow }
}
