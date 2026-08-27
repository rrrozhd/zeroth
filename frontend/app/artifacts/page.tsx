"use client";

import { useEffect, useMemo, useState } from "react";

import {
  Button,
  CodeBlock,
  ConsoleDataList,
  ConsoleDataRow,
  ConsoleEmpty,
  ConsoleField,
  ConsoleInput,
  ConsoleMeta,
  ConsoleNotice,
  ConsolePage,
  ConsolePageHeader,
  ConsoleSection,
  ConsoleSurface,
  Pill,
  Skeleton,
} from "@/app/components/primitives";
import { findArtifactReferences, type ArtifactReferenceSummary } from "@/app/lib/artifact-references";
import { getArtifact, listRuns, type RetrievedArtifact } from "@/app/lib/api";
import { isConfigured } from "@/app/lib/config";

type Preview =
  | { kind: "json" | "text"; text: string }
  | { kind: "image"; dataUrl: string }
  | { kind: "binary" };

function detectedMediaType(artifact: RetrievedArtifact): string {
  if (artifact.mediaType !== "application/octet-stream") return artifact.mediaType;
  const bytes = artifact.bytes;
  if (bytes.length >= 8 && [137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => bytes[index] === value)) {
    return "image/png";
  }
  if (bytes.length >= 3 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) {
    return "image/jpeg";
  }
  return artifact.mediaType;
}

function previewOf(artifact: RetrievedArtifact): Preview {
  const mediaType = detectedMediaType(artifact).split(";", 1)[0];
  if (mediaType === "image/png" || mediaType === "image/jpeg") {
    let binary = "";
    for (const byte of artifact.bytes) binary += String.fromCharCode(byte);
    return { kind: "image", dataUrl: `data:${mediaType};base64,${btoa(binary)}` };
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(artifact.bytes);
    if (mediaType === "application/json" || /^[\s]*[\[{]/.test(text)) {
      return { kind: "json", text: JSON.stringify(JSON.parse(text), null, 2) };
    }
    if (!text.includes("\u0000")) return { kind: "text", text };
  } catch {
    // Binary or malformed text remains downloadable without being rendered.
  }
  return { kind: "binary" };
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ArtifactsPage() {
  const [mounted, setMounted] = useState(false);
  const [artifactId, setArtifactId] = useState("");
  const [references, setReferences] = useState<ArtifactReferenceSummary[]>([]);
  const [discovering, setDiscovering] = useState(true);
  const [referenceError, setReferenceError] = useState(false);
  const [artifact, setArtifact] = useState<RetrievedArtifact | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const preview = useMemo(() => (artifact ? previewOf(artifact) : null), [artifact]);

  useEffect(() => {
    setMounted(true);
    if (!isConfigured()) {
      setDiscovering(false);
      return;
    }
    listRuns()
      .then((response) => {
        const found = new Map<string, ArtifactReferenceSummary>();
        for (const run of response.runs) {
          for (const reference of findArtifactReferences(run.terminal_output)) {
            found.set(reference.key, reference);
          }
        }
        setReferences(Array.from(found.values()));
      })
      .catch(() => {
        setReferences([]);
        setReferenceError(true);
      })
      .finally(() => setDiscovering(false));
  }, []);

  async function load(id = artifactId) {
    const normalized = id.trim();
    setError(null);
    if (!normalized) {
      setError("Enter an artifact ID before loading it.");
      return;
    }
    setArtifactId(normalized);
    setLoading(true);
    setArtifact(null);
    try {
      setArtifact(await getArtifact(normalized));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Artifact retrieval failed.");
    } finally {
      setLoading(false);
    }
  }

  function download() {
    if (!artifact) return;
    const blob = new Blob([new Uint8Array(artifact.bytes).buffer], {
      type: detectedMediaType(artifact),
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = artifact.artifactId.split("/").pop() || "artifact";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const connected = mounted && isConfigured();
  return (
    <ConsolePage>
      <ConsolePageHeader
        title="Artifacts"
        description="Inspect and retrieve output externalized by tenant-scoped workflow runs."
      />

      <ConsoleSection title="Retrieve an artifact">
        <ConsoleSurface>
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
            <ConsoleField
              label="Artifact ID"
              hint="Use the exact key recorded in a run output or evidence record."
              required
            >
              <ConsoleInput
                data-evidence-id="artifacts-id-input"
                value={artifactId}
                onChange={(event) => setArtifactId(event.target.value)}
                placeholder="run-id/node-id/artifact-key"
                onKeyDown={(event) => {
                  if (event.key === "Enter") void load();
                }}
              />
            </ConsoleField>
            <Button data-evidence-id="artifacts-load" variant="primary" onClick={() => void load()} disabled={loading || !connected}>
              {loading ? "Loading…" : "Load artifact"}
            </Button>
          </div>
          {!connected && mounted && (
            <ConsoleNotice>Connect to the API before retrieving protected artifacts.</ConsoleNotice>
          )}
          {error && <ConsoleNotice tone="danger" title="Artifact unavailable">{error}</ConsoleNotice>}
        </ConsoleSurface>
      </ConsoleSection>

      <ConsoleSection
        title="Recent run references"
        meta={references.length > 0 ? `${references.length} found` : undefined}
      >
        <ConsoleSurface density="flush">
          {discovering ? (
            <div className="p-4"><Skeleton height={54} /></div>
          ) : referenceError ? (
            <div className="p-4">
              <ConsoleNotice tone="danger" title="Artifact references unavailable">
                Zeroth could not load run references for the active tenant/workspace. Confirm the
                API connection is available and authenticated, and that your role can read runs.
              </ConsoleNotice>
            </div>
          ) : references.length === 0 ? (
            <ConsoleEmpty>No artifact references were found in this tenant&rsquo;s recent run outputs.</ConsoleEmpty>
          ) : (
            <ConsoleDataList ariaLabel="Recent artifact references">
              {references.map((reference) => (
                <ConsoleDataRow key={reference.key}>
                  <button
                    data-evidence-id="artifacts-recent-reference"
                    type="button"
                    className="min-w-0 flex-1 text-left"
                    onClick={() => void load(reference.key)}
                  >
                    <span className="block truncate font-mono text-sm">{reference.key}</span>
                    <span className="mt-1 block text-xs text-muted">
                      {reference.contentType ?? "content type determined on retrieval"}
                      {reference.size != null ? ` · ${formatBytes(reference.size)}` : ""}
                    </span>
                  </button>
                </ConsoleDataRow>
              ))}
            </ConsoleDataList>
          )}
        </ConsoleSurface>
      </ConsoleSection>

      <ConsoleSection title="Preview">
        <ConsoleSurface>
          {!artifact || !preview ? (
            <ConsoleEmpty>Load an artifact to inspect safe metadata and supported content.</ConsoleEmpty>
          ) : (
            <div className="space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1">
                  <div><ConsoleMeta>artifact · {artifact.artifactId}</ConsoleMeta></div>
                  <div><ConsoleMeta>content type · {detectedMediaType(artifact)}</ConsoleMeta></div>
                  <div><ConsoleMeta>size · {formatBytes(artifact.size)}</ConsoleMeta></div>
                </div>
                <div className="flex items-center gap-2">
                  <Pill tone="muted">tenant scoped</Pill>
                  <Button data-evidence-id="artifacts-download" onClick={download}>Download</Button>
                </div>
              </div>
              {preview.kind === "image" ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img data-evidence-id="artifacts-image-preview" src={preview.dataUrl} alt="Artifact preview" className="max-h-[520px] max-w-full rounded-xl border border-border object-contain" />
              ) : preview.kind === "binary" ? (
                <ConsoleNotice>Binary content is available for download but is not rendered in the console.</ConsoleNotice>
              ) : (
                <CodeBlock label={preview.kind === "json" ? "JSON preview" : "Text preview"} code={preview.text} />
              )}
            </div>
          )}
        </ConsoleSurface>
      </ConsoleSection>
    </ConsolePage>
  );
}
