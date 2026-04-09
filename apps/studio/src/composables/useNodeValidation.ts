import { computed } from 'vue'
import type { CanvasNode, CanvasEdge } from '../stores/canvas'
import { NODE_TYPE_REGISTRY } from '../types/nodes'

export interface ValidationIssue {
  type: 'missing_field' | 'invalid_connection' | 'type_mismatch'
  message: string
  field?: string
}

export function useNodeValidation(
  nodes: () => CanvasNode[],
  edges: () => CanvasEdge[]
) {
  const validationMap = computed(() => {
    const map = new Map<string, ValidationIssue[]>()
    for (const node of nodes()) {
      const issues: ValidationIssue[] = []
      const typeDef = NODE_TYPE_REGISTRY[node.type]
      if (!typeDef) continue

      // Check required properties
      if (typeDef.properties) {
        for (const prop of typeDef.properties) {
          if (prop.required && !node.data[prop.key]) {
            issues.push({
              type: 'missing_field',
              message: `${prop.label} is required`,
              field: prop.key,
            })
          }
        }
      }

      // Check minimum connections
      const nodeEdges = edges().filter(
        e => e.source === node.id || e.target === node.id
      )
      const inputPorts = typeDef.ports.filter(p => p.direction === 'input')
      const outputPorts = typeDef.ports.filter(p => p.direction === 'output')

      if (inputPorts.length > 0 && !nodeEdges.some(e => e.target === node.id)) {
        issues.push({
          type: 'invalid_connection',
          message: 'Missing input connection',
        })
      }
      if (outputPorts.length > 0 && !nodeEdges.some(e => e.source === node.id)) {
        issues.push({
          type: 'invalid_connection',
          message: 'Missing output connection',
        })
      }

      if (issues.length > 0) {
        map.set(node.id, issues)
      }
    }
    return map
  })

  function getIssues(nodeId: string): ValidationIssue[] {
    return validationMap.value.get(nodeId) ?? []
  }

  function isValid(nodeId: string): boolean {
    return !validationMap.value.has(nodeId)
  }

  return { validationMap, getIssues, isValid }
}
