import { onMounted, onUnmounted } from 'vue'
import { useCanvasStore } from '../stores/canvas'

export function useKeyboardShortcuts() {
  const canvasStore = useCanvasStore()

  function isInputElement(target: EventTarget | null): boolean {
    if (!target || !(target instanceof HTMLElement)) return false
    const tag = target.tagName.toLowerCase()
    return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable
  }

  function handleKeydown(e: KeyboardEvent) {
    // Guard: skip shortcuts when typing in form fields
    if (isInputElement(e.target)) return

    const isMod = e.ctrlKey || e.metaKey

    // Delete/Backspace: remove selected (no modifier required)
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault()
      canvasStore.deleteSelected()
    }
    // Ctrl+Z: undo
    else if (isMod && e.key === 'z' && !e.shiftKey) {
      e.preventDefault()
      canvasStore.undo()
    }
    // Ctrl+Shift+Z: redo
    else if (isMod && (e.key === 'z' || e.key === 'Z') && e.shiftKey) {
      e.preventDefault()
      canvasStore.redo()
    }
    // Ctrl+A: select all
    else if (isMod && e.key === 'a') {
      e.preventDefault()
      canvasStore.selectAll()
    }
    // Ctrl+C: copy
    else if (isMod && e.key === 'c') {
      e.preventDefault()
      canvasStore.copySelected()
    }
    // Ctrl+V: paste
    else if (isMod && e.key === 'v') {
      e.preventDefault()
      canvasStore.pasteClipboard()
    }
    // Ctrl+D: duplicate
    else if (isMod && e.key === 'd') {
      e.preventDefault()
      canvasStore.duplicateSelected()
    }
  }

  onMounted(() => window.addEventListener('keydown', handleKeydown))
  onUnmounted(() => window.removeEventListener('keydown', handleKeydown))

  return { handleKeydown }
}
