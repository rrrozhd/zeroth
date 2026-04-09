import { useVueFlow } from '@vue-flow/core'
import { useCanvasStore } from '../stores/canvas'
import { NODE_TYPE_REGISTRY } from '../types/nodes'

export function useCanvasActions() {
  const { fitView, screenToFlowCoordinate } = useVueFlow()
  const canvasStore = useCanvasStore()

  function addNodeAtCenter(type: string) {
    const typeKeys = Object.keys(NODE_TYPE_REGISTRY)
    const nodeType = typeKeys.includes(type) ? type : (typeKeys[0] ?? 'agent')
    // Place in center of current viewport using proper coordinate conversion
    const position = screenToFlowCoordinate({ x: window.innerWidth / 2, y: window.innerHeight / 2 })
    canvasStore.addNode(nodeType, position)
  }

  function fitToView() {
    fitView({ padding: 0.2, duration: 300 })
  }

  function deleteSelected(nodeId: string) {
    canvasStore.removeNode(nodeId)
  }

  return { addNodeAtCenter, fitToView, deleteSelected }
}
