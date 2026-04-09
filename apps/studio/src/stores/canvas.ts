import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Connection, XYPosition } from '@vue-flow/core'
import { NODE_TYPE_REGISTRY } from '../types/nodes'

export interface CanvasNode {
  id: string
  type: string
  position: XYPosition
  data: { label: string; nodeType: string; [key: string]: unknown }
  selected?: boolean
  [key: string]: unknown
}

export interface CanvasEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string
  targetHandle?: string
  [key: string]: unknown
}

export interface CanvasCommand {
  execute(): void
  undo(): void
  description: string
}

export const useCanvasStore = defineStore('canvas', () => {
  const nodes = ref<CanvasNode[]>([])
  const edges = ref<CanvasEdge[]>([])

  const history = ref<CanvasCommand[]>([])
  const historyIndex = ref(-1)
  const MAX_HISTORY = 50
  const isExecutingCommand = ref(false)

  const canUndo = computed(() => historyIndex.value >= 0)
  const canRedo = computed(() => historyIndex.value < history.value.length - 1)

  let nodeCounter = 0

  // --- Clipboard ---
  const clipboard = ref<{ nodes: CanvasNode[]; edges: CanvasEdge[] } | null>(null)

  // --- Command infrastructure ---

  function executeCommand(command: CanvasCommand) {
    // Truncate redo history
    history.value = history.value.slice(0, historyIndex.value + 1)
    isExecutingCommand.value = true
    command.execute()
    isExecutingCommand.value = false
    history.value.push(command)
    if (history.value.length > MAX_HISTORY) {
      history.value.shift()
    } else {
      historyIndex.value++
    }
  }

  function undo() {
    if (!canUndo.value) return
    isExecutingCommand.value = true
    history.value[historyIndex.value]!.undo()
    isExecutingCommand.value = false
    historyIndex.value--
  }

  function redo() {
    if (!canRedo.value) return
    historyIndex.value++
    isExecutingCommand.value = true
    history.value[historyIndex.value]!.execute()
    isExecutingCommand.value = false
  }

  function clearHistory() {
    history.value = []
    historyIndex.value = -1
  }

  // --- Raw mutations (backward-compatible, no undo) ---

  function addNode(nodeOrType: string | CanvasNode, position?: XYPosition) {
    if (typeof nodeOrType === 'string') {
      const type = nodeOrType
      const typeDef = NODE_TYPE_REGISTRY[type]
      if (!typeDef) return
      const id = `${type}-${++nodeCounter}-${Date.now()}`
      nodes.value.push({
        id,
        type,
        position: position ?? { x: 0, y: 0 },
        data: { label: typeDef.label, nodeType: type },
      })
      return id
    } else {
      nodes.value.push(nodeOrType)
      return nodeOrType.id
    }
  }

  function removeNode(id: string) {
    nodes.value = nodes.value.filter(n => n.id !== id)
    edges.value = edges.value.filter(e => e.source !== id && e.target !== id)
  }

  function addEdge(connectionOrEdge: Connection | CanvasEdge) {
    if ('id' in connectionOrEdge) {
      edges.value.push(connectionOrEdge)
      return connectionOrEdge.id
    } else {
      const connection = connectionOrEdge
      const id = `edge-${connection.source}-${connection.target}-${Date.now()}`
      edges.value.push({
        id,
        source: connection.source,
        target: connection.target,
        sourceHandle: connection.sourceHandle ?? undefined,
        targetHandle: connection.targetHandle ?? undefined,
      })
      return id
    }
  }

  function removeEdge(id: string) {
    edges.value = edges.value.filter(e => e.id !== id)
  }

  function clearCanvas() {
    nodes.value = []
    edges.value = []
  }

  // --- Command-based mutations (with undo) ---

  function addNodeWithUndo(type: string, position: XYPosition) {
    const typeDef = NODE_TYPE_REGISTRY[type]
    if (!typeDef) return
    let createdId: string | undefined

    const command: CanvasCommand = {
      description: `Add ${typeDef.label} node`,
      execute() {
        if (createdId) {
          // Re-executing (redo) - restore the same node
          const existingNode = nodes.value.find(n => n.id === createdId)
          if (!existingNode) {
            const id = createdId
            nodes.value.push({
              id,
              type,
              position: { ...position },
              data: { label: typeDef.label, nodeType: type },
            })
          }
        } else {
          createdId = addNode(type, position) ?? undefined
        }
      },
      undo() {
        if (createdId) {
          removeNode(createdId)
        }
      },
    }
    executeCommand(command)
    return createdId
  }

  function removeNodeWithUndo(id: string) {
    const node = nodes.value.find(n => n.id === id)
    if (!node) return
    let snapshotNode: CanvasNode
    let snapshotEdges: CanvasEdge[]

    const command: CanvasCommand = {
      description: `Remove ${node.data.label} node`,
      execute() {
        // Snapshot before removal
        const n = nodes.value.find(n => n.id === id)
        if (n) {
          snapshotNode = JSON.parse(JSON.stringify(n))
          snapshotEdges = edges.value
            .filter(e => e.source === id || e.target === id)
            .map(e => JSON.parse(JSON.stringify(e)))
          removeNode(id)
        }
      },
      undo() {
        if (snapshotNode) {
          nodes.value.push(JSON.parse(JSON.stringify(snapshotNode)))
          for (const edge of snapshotEdges) {
            edges.value.push(JSON.parse(JSON.stringify(edge)))
          }
        }
      },
    }
    executeCommand(command)
  }

  function addEdgeWithUndo(connection: Connection) {
    let createdId: string | undefined
    let snapshotEdge: CanvasEdge | undefined

    const command: CanvasCommand = {
      description: 'Add edge',
      execute() {
        if (snapshotEdge) {
          edges.value.push(JSON.parse(JSON.stringify(snapshotEdge)))
        } else {
          createdId = addEdge(connection) ?? undefined
          if (createdId) {
            snapshotEdge = edges.value.find(e => e.id === createdId)
          }
        }
      },
      undo() {
        if (createdId) {
          removeEdge(createdId)
        }
      },
    }
    executeCommand(command)
  }

  function removeEdgeWithUndo(id: string) {
    const edge = edges.value.find(e => e.id === id)
    if (!edge) return
    let snapshotEdge: CanvasEdge

    const command: CanvasCommand = {
      description: 'Remove edge',
      execute() {
        const e = edges.value.find(e => e.id === id)
        if (e) {
          snapshotEdge = JSON.parse(JSON.stringify(e))
          removeEdge(id)
        }
      },
      undo() {
        if (snapshotEdge) {
          edges.value.push(JSON.parse(JSON.stringify(snapshotEdge)))
        }
      },
    }
    executeCommand(command)
  }

  function moveNodeWithUndo(id: string, from: XYPosition, to: XYPosition) {
    const command: CanvasCommand = {
      description: 'Move node',
      execute() {
        const node = nodes.value.find(n => n.id === id)
        if (node) {
          node.position = { ...to }
        }
      },
      undo() {
        const node = nodes.value.find(n => n.id === id)
        if (node) {
          node.position = { ...from }
        }
      },
    }
    executeCommand(command)
  }

  function updateNodePropertyWithUndo(nodeId: string, key: string, oldValue: unknown, newValue: unknown) {
    const command: CanvasCommand = {
      description: `Update ${key}`,
      execute() {
        const node = nodes.value.find(n => n.id === nodeId)
        if (node) {
          node.data[key] = newValue
        }
      },
      undo() {
        const node = nodes.value.find(n => n.id === nodeId)
        if (node) {
          node.data[key] = oldValue
        }
      },
    }
    executeCommand(command)
  }

  function deleteSelected() {
    const selectedNodes = nodes.value.filter(n => n.selected)
    if (selectedNodes.length === 0) return

    // Snapshot all selected nodes and their connected edges
    const snapshots: { node: CanvasNode; edges: CanvasEdge[] }[] = []

    const command: CanvasCommand = {
      description: `Delete ${selectedNodes.length} node(s)`,
      execute() {
        for (const node of [...nodes.value].filter(n => n.selected)) {
          const connectedEdges = edges.value
            .filter(e => e.source === node.id || e.target === node.id)
            .map(e => JSON.parse(JSON.stringify(e)))
          snapshots.push({
            node: JSON.parse(JSON.stringify(node)),
            edges: connectedEdges,
          })
          removeNode(node.id)
        }
      },
      undo() {
        for (const snapshot of snapshots) {
          nodes.value.push(JSON.parse(JSON.stringify(snapshot.node)))
          for (const edge of snapshot.edges) {
            // Only restore edge if not already present
            if (!edges.value.find(e => e.id === edge.id)) {
              edges.value.push(JSON.parse(JSON.stringify(edge)))
            }
          }
        }
        snapshots.length = 0
      },
    }
    executeCommand(command)
  }

  function selectAll() {
    for (const node of nodes.value) {
      node.selected = true
    }
  }

  function copySelected() {
    const selectedNodes = nodes.value.filter(n => n.selected)
    if (selectedNodes.length === 0) return

    const selectedIds = new Set(selectedNodes.map(n => n.id))
    const interEdges = edges.value.filter(
      e => selectedIds.has(e.source) && selectedIds.has(e.target),
    )

    clipboard.value = {
      nodes: selectedNodes.map(n => JSON.parse(JSON.stringify(n))),
      edges: interEdges.map(e => JSON.parse(JSON.stringify(e))),
    }
  }

  function pasteClipboard() {
    if (!clipboard.value || clipboard.value.nodes.length === 0) return

    const OFFSET = 24
    const idMap = new Map<string, string>()

    // Create new IDs for each node
    for (const node of clipboard.value.nodes) {
      const newId = `${node.type}-${++nodeCounter}-${Date.now()}`
      idMap.set(node.id, newId)
    }

    const newNodes: CanvasNode[] = []
    const newEdges: CanvasEdge[] = []

    for (const node of clipboard.value.nodes) {
      const newId = idMap.get(node.id)!
      newNodes.push({
        ...JSON.parse(JSON.stringify(node)),
        id: newId,
        position: { x: node.position.x + OFFSET, y: node.position.y + OFFSET },
        selected: false,
      })
    }

    for (const edge of clipboard.value.edges) {
      const newSource = idMap.get(edge.source)
      const newTarget = idMap.get(edge.target)
      if (newSource && newTarget) {
        newEdges.push({
          ...JSON.parse(JSON.stringify(edge)),
          id: `edge-${newSource}-${newTarget}-${Date.now()}`,
          source: newSource,
          target: newTarget,
        })
      }
    }

    const command: CanvasCommand = {
      description: `Paste ${newNodes.length} node(s)`,
      execute() {
        for (const node of newNodes) {
          nodes.value.push(JSON.parse(JSON.stringify(node)))
        }
        for (const edge of newEdges) {
          edges.value.push(JSON.parse(JSON.stringify(edge)))
        }
      },
      undo() {
        const nodeIds = new Set(newNodes.map(n => n.id))
        const edgeIds = new Set(newEdges.map(e => e.id))
        nodes.value = nodes.value.filter(n => !nodeIds.has(n.id))
        edges.value = edges.value.filter(e => !edgeIds.has(e.id))
      },
    }
    executeCommand(command)
  }

  function duplicateSelected() {
    copySelected()
    pasteClipboard()
  }

  return {
    // State
    nodes,
    edges,
    // Raw mutations (backward-compatible)
    addNode,
    removeNode,
    addEdge,
    removeEdge,
    clearCanvas,
    // Command pattern
    executeCommand,
    undo,
    redo,
    canUndo,
    canRedo,
    isExecutingCommand,
    clearHistory,
    // Command-based mutations
    addNodeWithUndo,
    removeNodeWithUndo,
    addEdgeWithUndo,
    removeEdgeWithUndo,
    moveNodeWithUndo,
    updateNodePropertyWithUndo,
    // Selection & clipboard
    deleteSelected,
    selectAll,
    copySelected,
    pasteClipboard,
    duplicateSelected,
  }
})
