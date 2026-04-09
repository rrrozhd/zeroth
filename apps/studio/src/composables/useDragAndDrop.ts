import { useVueFlow } from '@vue-flow/core'
import { useCanvasStore } from '../stores/canvas'

export function useDragAndDrop() {
  const { screenToFlowCoordinate } = useVueFlow()
  const canvasStore = useCanvasStore()

  function onDragOver(event: DragEvent) {
    event.preventDefault()
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'move'
    }
  }

  function onDrop(event: DragEvent) {
    const type = event.dataTransfer?.getData('application/vueflow')
    if (!type) return
    const position = screenToFlowCoordinate({
      x: event.clientX,
      y: event.clientY,
    })
    canvasStore.addNodeWithUndo(type, position)
  }

  return { onDragOver, onDrop }
}
