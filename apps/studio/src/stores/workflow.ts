import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import * as api from '../api/workflows'

export const useWorkflowStore = defineStore('workflow', () => {
  const currentWorkflowId = ref<string | null>(null)
  const currentWorkflowName = ref<string>('')
  const currentVersion = ref<number>(1)
  const isDirty = ref(false)
  const isSaving = ref(false)
  const lastSavedAt = ref<string | null>(null)
  const workflows = ref<
    Array<{ id: string; name: string; version: number; status: string; updatedAt: string }>
  >([])
  const error = ref<string | null>(null)

  const hasWorkflow = computed(() => currentWorkflowId.value !== null)

  async function fetchWorkflows() {
    try {
      error.value = null
      const result = await api.listWorkflows()
      workflows.value = result.map((w) => ({
        id: w.id,
        name: w.name,
        version: w.version,
        status: w.status,
        updatedAt: w.updated_at,
      }))
    } catch (_e: unknown) {
      error.value =
        'Cannot reach the Zeroth API. Verify the backend is running and try again.'
    }
  }

  async function createNew(name: string) {
    try {
      error.value = null
      const result = await api.createWorkflow(name)
      currentWorkflowId.value = result.id
      currentWorkflowName.value = result.name
      currentVersion.value = result.version
      isDirty.value = false
      lastSavedAt.value = result.updated_at
      await fetchWorkflows()
      return result
    } catch (_e: unknown) {
      error.value = 'Could not save workflow. Check your connection and try again.'
      return null
    }
  }

  async function loadWorkflow(id: string) {
    try {
      error.value = null
      const result = await api.getWorkflow(id)
      currentWorkflowId.value = result.id
      currentWorkflowName.value = result.name
      currentVersion.value = result.version
      isDirty.value = false
      lastSavedAt.value = result.updated_at
      return result
    } catch (_e: unknown) {
      error.value =
        'Failed to load workflow. The server may be temporarily unavailable. Refresh the page to retry.'
      return null
    }
  }

  function markDirty() {
    isDirty.value = true
  }

  function setCurrentId(id: string | null) {
    currentWorkflowId.value = id
  }

  return {
    currentWorkflowId,
    currentWorkflowName,
    currentVersion,
    isDirty,
    isSaving,
    lastSavedAt,
    workflows,
    error,
    hasWorkflow,
    fetchWorkflows,
    createNew,
    loadWorkflow,
    markDirty,
    setCurrentId,
  }
})
