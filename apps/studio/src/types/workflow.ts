export interface WorkflowSummary {
  id: string
  name: string
  version: number
  status: 'draft' | 'published' | 'archived'
  updatedAt: string
}

export interface StudioNode {
  id: string
  type: string
  position: { x: number; y: number }
  data: { label: string; nodeType: string }
}

export interface StudioEdge {
  id: string
  source: string
  target: string
  sourceHandle: string
  targetHandle: string
}

export interface StudioWorkflow {
  id: string
  name: string
  version: number
  status: string
  nodes: StudioNode[]
  edges: StudioEdge[]
  viewport: { x: number; y: number; zoom: number }
}
