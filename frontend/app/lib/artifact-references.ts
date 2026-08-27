export type ArtifactReferenceSummary = {
  key: string;
  contentType: string | null;
  size: number | null;
};

export function findArtifactReferences(value: unknown): ArtifactReferenceSummary[] {
  const found = new Map<string, ArtifactReferenceSummary>();

  function visit(candidate: unknown, artifactContext: boolean): void {
    if (Array.isArray(candidate)) {
      for (const child of candidate) visit(child, artifactContext);
      return;
    }
    if (candidate == null || typeof candidate !== "object") return;
    const object = candidate as Record<string, unknown>;
    if (artifactContext && typeof object.key === "string" && object.key.trim()) {
      const key = object.key.trim();
      found.set(key, {
        key,
        contentType:
          typeof object.content_type === "string"
            ? object.content_type
            : typeof object.media_type === "string"
              ? object.media_type
              : null,
        size:
          typeof object.size === "number" && Number.isFinite(object.size)
            ? object.size
            : null,
      });
    }
    for (const [key, child] of Object.entries(object)) {
      visit(child, key === "artifact" || key === "artifacts");
    }
  }

  visit(value, false);
  return Array.from(found.values());
}
