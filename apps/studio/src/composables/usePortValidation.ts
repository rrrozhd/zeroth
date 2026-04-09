import type { Connection } from '@vue-flow/core'
import type { PortType } from '../types/nodes'

function getPortType(handleId: string | null | undefined): PortType {
  if (!handleId) return 'any'
  // Handle IDs follow pattern: "input-data", "output-control", "output-true", "output-false"
  const parts = handleId.split('-')
  // The type is everything after the direction prefix (input- or output-)
  const typeStr = parts.slice(1).join('-')
  // Map compound types: "true" and "false" from conditionBranch are data-type outputs
  if (typeStr === 'true' || typeStr === 'false') return 'data'
  return (typeStr as PortType) || 'any'
}

export function isValidConnection(connection: Connection): boolean {
  const sourceType = getPortType(connection.sourceHandle)
  const targetType = getPortType(connection.targetHandle)
  // Same type or either is 'any'
  return sourceType === targetType || sourceType === 'any' || targetType === 'any'
}

export function usePortValidation() {
  return { isValidConnection, getPortType }
}
