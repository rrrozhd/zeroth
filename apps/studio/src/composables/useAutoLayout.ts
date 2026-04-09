import dagre from '@dagrejs/dagre'
import type { XYPosition } from '@vue-flow/core'
import { useCanvasStore } from '../stores/canvas'

const NODE_WIDTH = 160   // matches BaseNode 10rem
const NODE_HEIGHT = 100  // approximate node height

export function useAutoLayout() {
  const canvasStore = useCanvasStore()

  function applyLayout(direction: 'TB' | 'LR' = 'TB') {
    const currentNodes = canvasStore.nodes
    const currentEdges = canvasStore.edges
    if (currentNodes.length === 0) return

    const g = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
    g.setGraph({ rankdir: direction, nodesep: 60, ranksep: 80 })

    currentNodes.forEach(node => {
      g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT })
    })
    currentEdges.forEach(edge => {
      g.setEdge(edge.source, edge.target)
    })

    dagre.layout(g)

    // Collect old and new positions, then apply as batch move commands
    const moves: { id: string; from: XYPosition; to: XYPosition }[] = []
    currentNodes.forEach(node => {
      const pos = g.node(node.id)
      if (!pos) return
      const newPos: XYPosition = {
        x: pos.x - NODE_WIDTH / 2,
        y: pos.y - NODE_HEIGHT / 2,
      }
      // Only move if position actually changed
      if (Math.abs(node.position.x - newPos.x) > 1 || Math.abs(node.position.y - newPos.y) > 1) {
        moves.push({
          id: node.id,
          from: { x: node.position.x, y: node.position.y },
          to: newPos,
        })
      }
    })

    // Apply all moves as a single compound command so undo restores all positions
    if (moves.length === 0) return

    canvasStore.executeCommand({
      description: `Auto-layout (${direction})`,
      execute() {
        moves.forEach(m => {
          const node = canvasStore.nodes.find(n => n.id === m.id)
          if (node) {
            node.position = { ...m.to }
          }
        })
      },
      undo() {
        moves.forEach(m => {
          const node = canvasStore.nodes.find(n => n.id === m.id)
          if (node) {
            node.position = { ...m.from }
          }
        })
      },
    })
  }

  return { applyLayout }
}
