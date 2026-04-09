export type PortType = 'data' | 'control' | 'memory' | 'any'
export type PortDirection = 'input' | 'output'

export interface PortDefinition {
  id: string
  type: PortType
  direction: PortDirection
  label: string
}

export interface NodeTypeDefinition {
  type: string
  label: string
  category: 'core' | 'flow' | 'data'
  ports: PortDefinition[]
  icon: string
}

export const NODE_TYPE_REGISTRY: Record<string, NodeTypeDefinition> = {
  start: {
    type: 'start',
    label: 'Start',
    category: 'flow',
    icon: 'lightning',
    ports: [
      { id: 'output-control', type: 'control', direction: 'output', label: 'Out' },
    ],
  },
  end: {
    type: 'end',
    label: 'End',
    category: 'flow',
    icon: 'arrow-to-dot',
    ports: [
      { id: 'input-control', type: 'control', direction: 'input', label: 'In' },
    ],
  },
  agent: {
    type: 'agent',
    label: 'Agent',
    category: 'core',
    icon: 'sparkle',
    ports: [
      { id: 'input-data', type: 'data', direction: 'input', label: 'Input' },
      { id: 'output-data', type: 'data', direction: 'output', label: 'Output' },
    ],
  },
  executionUnit: {
    type: 'executionUnit',
    label: 'Execution Unit',
    category: 'core',
    icon: 'outgoing-arrow',
    ports: [
      { id: 'input-data', type: 'data', direction: 'input', label: 'Input' },
      { id: 'output-data', type: 'data', direction: 'output', label: 'Output' },
    ],
  },
  approvalGate: {
    type: 'approvalGate',
    label: 'Approval Gate',
    category: 'core',
    icon: 'shield-check',
    ports: [
      { id: 'input-control', type: 'control', direction: 'input', label: 'In' },
      { id: 'output-control', type: 'control', direction: 'output', label: 'Out' },
    ],
  },
  memoryResource: {
    type: 'memoryResource',
    label: 'Memory Resource',
    category: 'data',
    icon: 'database',
    ports: [
      { id: 'input-data', type: 'data', direction: 'input', label: 'Input' },
      { id: 'output-data', type: 'data', direction: 'output', label: 'Output' },
    ],
  },
  conditionBranch: {
    type: 'conditionBranch',
    label: 'Condition Branch',
    category: 'flow',
    icon: 'diamond-fork',
    ports: [
      { id: 'input-data', type: 'data', direction: 'input', label: 'Input' },
      { id: 'output-true', type: 'data', direction: 'output', label: 'True' },
      { id: 'output-false', type: 'data', direction: 'output', label: 'False' },
    ],
  },
  dataMapping: {
    type: 'dataMapping',
    label: 'Data Mapping',
    category: 'data',
    icon: 'mapping',
    ports: [
      { id: 'input-data', type: 'data', direction: 'input', label: 'Input' },
      { id: 'output-data', type: 'data', direction: 'output', label: 'Output' },
    ],
  },
}
