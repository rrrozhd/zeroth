// Remembers the workflow last opened in the editor so the "Studio" nav link
// can jump straight back to it. Cleared explicitly (the editor's "← Studio"
// back link) or when the stored workflow no longer exists (404 on load).

const LAST_KEY = "zeroth.studio.lastWorkflow";

export function getLastWorkflowId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(LAST_KEY);
}

export function setLastWorkflowId(id: string | null): void {
  if (typeof window === "undefined") return;
  if (id === null) window.localStorage.removeItem(LAST_KEY);
  else window.localStorage.setItem(LAST_KEY, id);
}
