import { ref } from 'vue'
import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', () => {
  const railCollapsed = ref(false)
  const selectedNodeId = ref<string | null>(null)
  const currentMode = ref<'editor' | 'executions'>('editor')

  function toggleRail() {
    railCollapsed.value = !railCollapsed.value
  }

  function setMode(mode: 'editor' | 'executions') {
    currentMode.value = mode
  }

  function selectNode(id: string | null) {
    selectedNodeId.value = id
  }

  return { railCollapsed, selectedNodeId, currentMode, toggleRail, setMode, selectNode }
})
