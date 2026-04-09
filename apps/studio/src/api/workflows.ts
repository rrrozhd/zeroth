import { apiFetch } from './client'

// Response types matching backend studio_schemas.py
export interface WorkflowSummaryResponse {
  id: string
  name: string
  version: number
  status: string
  updated_at: string
}

export interface StudioNodeResponse {
  id: string
  type: string
  position: { x: number; y: number }
  data: Record<string, unknown>
}

export interface StudioEdgeResponse {
  id: string
  source: string
  target: string
  source_handle: string | null
  target_handle: string | null
}

export interface StudioViewportResponse {
  x: number
  y: number
  zoom: number
}

export interface WorkflowDetailResponse {
  id: string
  name: string
  version: number
  status: string
  nodes: StudioNodeResponse[]
  edges: StudioEdgeResponse[]
  viewport: StudioViewportResponse
  updated_at: string
}

export async function listWorkflows(): Promise<WorkflowSummaryResponse[]> {
  return apiFetch<WorkflowSummaryResponse[]>('/workflows')
}

export async function createWorkflow(name: string): Promise<WorkflowDetailResponse> {
  return apiFetch<WorkflowDetailResponse>('/workflows', {
    method: 'POST',
    body: JSON.stringify({ name }),
  })
}

export async function getWorkflow(id: string): Promise<WorkflowDetailResponse> {
  return apiFetch<WorkflowDetailResponse>(`/workflows/${id}`)
}

export async function updateWorkflow(
  id: string,
  data: {
    name?: string
    nodes?: StudioNodeResponse[]
    edges?: StudioEdgeResponse[]
    viewport?: StudioViewportResponse
  },
): Promise<WorkflowDetailResponse> {
  return apiFetch<WorkflowDetailResponse>(`/workflows/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
  })
}

export async function deleteWorkflow(id: string): Promise<void> {
  return apiFetch<void>(`/workflows/${id}`, { method: 'DELETE' })
}
