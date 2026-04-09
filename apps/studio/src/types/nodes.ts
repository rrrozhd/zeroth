export type PortType = 'data' | 'control' | 'memory' | 'any'
export type PortDirection = 'input' | 'output'

export interface PortDefinition {
  id: string
  type: PortType
  direction: PortDirection
  label: string
}

export type PropertyFieldType = 'text' | 'textarea' | 'number' | 'select' | 'toggle'

export interface PropertyDefinition {
  key: string
  label: string
  type: PropertyFieldType
  required?: boolean
  default?: unknown
  options?: { label: string; value: string }[]
  placeholder?: string
}

export interface NodeTypeDefinition {
  type: string
  label: string
  category: 'core' | 'flow' | 'data'
  ports: PortDefinition[]
  icon: string
  properties?: PropertyDefinition[]
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Start node name' },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'End node name' },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Agent name' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'model', label: 'Model', type: 'select', options: [
        { label: 'GPT-4o', value: 'gpt-4o' },
        { label: 'Claude 3.5 Sonnet', value: 'claude-3.5-sonnet' },
        { label: 'Claude 3 Opus', value: 'claude-3-opus' },
      ] },
      { key: 'temperature', label: 'Temperature', type: 'number', default: 0.7 },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Unit name' },
      { key: 'description', label: 'Description', type: 'textarea' },
      { key: 'sandbox', label: 'Sandbox', type: 'toggle', default: true },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Gate name' },
      { key: 'approver', label: 'Approver', type: 'text', placeholder: 'Approver email or role' },
      { key: 'slaMinutes', label: 'SLA (minutes)', type: 'number', default: 60 },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Resource name' },
      { key: 'connectorType', label: 'Connector', type: 'select', options: [
        { label: 'Vector Store', value: 'vector' },
        { label: 'Key-Value', value: 'kv' },
        { label: 'Document', value: 'document' },
      ] },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Condition name' },
      { key: 'expression', label: 'Expression', type: 'textarea', required: true, placeholder: 'Condition expression' },
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
    properties: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Mapping name' },
      { key: 'description', label: 'Description', type: 'textarea' },
    ],
  },
}

export type PaletteCategory = 'agents' | 'logic' | 'data' | 'lifecycle'

export const PALETTE_CATEGORIES: Record<PaletteCategory, { label: string; nodeTypes: string[] }> = {
  agents: { label: 'Agents', nodeTypes: ['agent', 'executionUnit'] },
  logic: { label: 'Logic', nodeTypes: ['conditionBranch', 'approvalGate'] },
  data: { label: 'Data', nodeTypes: ['memoryResource', 'dataMapping'] },
  lifecycle: { label: 'Lifecycle', nodeTypes: ['start', 'end'] },
}
