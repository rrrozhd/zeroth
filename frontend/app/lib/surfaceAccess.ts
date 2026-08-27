export function isForbiddenSurface(message: string | null): boolean {
  return message !== null && /^403(?:\s|$)/.test(message.trim());
}

export function surfaceAccessMessage(message: string, surface: string): string {
  if (!isForbiddenSurface(message)) return message;
  const resource = surface.toLowerCase().replace(/s$/, "");
  return `${surface} are hidden because this API key cannot read ${resource} administration data.`;
}
