"use client";

import "@xyflow/react/dist/style.css";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  addEdge,
  Background,
  Controls,
  MarkerType,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type ReactFlowInstance,
} from "@xyflow/react";
import { DEFAULT_CONFIG, NodeInspector } from "@/app/components/NodeInspector";
import { NODE_META, NodeGlyph } from "@/app/components/nodeMeta";
import { StudioNodeView, type Port } from "@/app/components/StudioNodeView";
import { Button, Card, ErrorBox } from "@/app/components/ui";
import {
  cloneWorkflow,
  errMsg,
  getWorkflow,
  listNodeTypes,
  updateWorkflow,
  type NodeType,
  type StudioEdge,
  type StudioNode,
  type WorkflowDetail,
} from "@/app/lib/api";

const nodeTypes = { studio: StudioNodeView };

type Cfg = Record<string, unknown>;

function portsFor(type: string, types: NodeType[]): Port[] {
  return (types.find((t) => t.type === type)?.ports ?? []) as Port[];
}

function toRfNodes(detail: WorkflowDetail, types: NodeType[]): Node[] {
  return detail.nodes.map((n) => {
    const data = (n.data ?? {}) as { label?: string; config?: Cfg };
    return {
      id: n.id,
      type: "studio",
      position: { x: n.position.x, y: n.position.y },
      data: {
        label: data.label || n.id,
        studioType: n.type,
        ports: portsFor(n.type, types),
        config: data.config ?? {},
      },
    };
  });
}

function toRfEdges(detail: WorkflowDetail): Edge[] {
  return detail.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.source_handle ?? undefined,
    targetHandle: e.target_handle ?? undefined,
  }));
}

function toStudioNodes(nodes: Node[]): StudioNode[] {
  return nodes.map((n) => {
    const d = n.data as { studioType: string; label: string; config: Cfg };
    return {
      id: n.id,
      type: d.studioType,
      position: { x: n.position.x, y: n.position.y },
      data: { label: d.label, config: d.config },
    };
  });
}

function toStudioEdges(edges: Edge[]): StudioEdge[] {
  return edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    source_handle: e.sourceHandle ?? null,
    target_handle: e.targetHandle ?? null,
  }));
}

export default function StudioEditPage() {
  const [id, setId] = useState<string | null>(null);
  useEffect(() => {
    setId(new URLSearchParams(window.location.search).get("id"));
  }, []);

  if (!id) {
    return (
      <div className="space-y-4">
        <Link href="/studio" className="text-sm text-zinc-500 hover:underline">
          ← Back to Studio
        </Link>
        <ErrorBox message="No workflow id in the URL (?id=…)." />
      </div>
    );
  }

  return (
    <ReactFlowProvider>
      <Editor id={id} />
    </ReactFlowProvider>
  );
}

function Editor({ id }: { id: string }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [name, setName] = useState("");
  const [status, setStatus] = useState<string>("");
  const [palette, setPalette] = useState<NodeType[]>([]);
  const [rf, setRf] = useState<ReactFlowInstance<Node, Edge> | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const paneRef = useRef<HTMLDivElement>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [cloning, setCloning] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [detail, types] = await Promise.all([getWorkflow(id), listNodeTypes()]);
      setName(detail.name);
      setStatus(detail.status);
      setPalette(types);
      setNodes(toRfNodes(detail, types));
      setEdges(toRfEdges(detail));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, [id, setNodes, setEdges]);

  useEffect(() => {
    load();
  }, [load]);

  const onConnect = useCallback(
    (c: Connection) =>
      setEdges((es) =>
        addEdge(
          { ...c, id: `e-${c.source}.${c.sourceHandle}-${c.target}.${c.targetHandle}` },
          es,
        ),
      ),
    [setEdges],
  );

  const addNode = useCallback(
    (t: NodeType) => {
      const newId = `${t.type}-${Date.now().toString(36)}`;
      // Drop the node near the center of what the user is currently looking at
      // (in flow space), with a small stagger so repeated adds don't stack.
      const rect = paneRef.current?.getBoundingClientRect();
      const center =
        rf && rect
          ? rf.screenToFlowPosition({
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
            })
          : null;
      setNodes((ns) => {
        const jitter = (ns.length % 5) * 28;
        const position = center
          ? { x: center.x - 75 + jitter, y: center.y - 24 + jitter }
          : { x: 80 + ns.length * 30, y: 80 + ns.length * 30 };
        return ns.concat({
          id: newId,
          type: "studio",
          position,
          data: {
            label: t.label,
            studioType: t.type,
            ports: t.ports,
            config: { ...(DEFAULT_CONFIG[t.type] ?? {}) },
          },
        });
      });
    },
    [rf, setNodes],
  );

  const patchNode = useCallback(
    (nodeId: string, patch: Partial<{ label: string; config: Cfg }>) => {
      setNodes((ns) =>
        ns.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, ...patch } } : n)),
      );
    },
    [setNodes],
  );

  const deleteNode = useCallback(
    (nodeId: string) => {
      setNodes((ns) => ns.filter((n) => n.id !== nodeId));
      setEdges((es) => es.filter((e) => e.source !== nodeId && e.target !== nodeId));
      setEditingId(null);
    },
    [setNodes, setEdges],
  );

  async function save() {
    setSaveState("saving");
    setError(null);
    try {
      await updateWorkflow(id, {
        name,
        nodes: toStudioNodes(nodes),
        edges: toStudioEdges(edges),
        viewport: rf ? rf.getViewport() : { x: 0, y: 0, zoom: 1 },
      });
      setSaveState("saved");
      window.setTimeout(() => setSaveState("idle"), 1500);
    } catch (e) {
      setError(errMsg(e));
      setSaveState("idle");
    }
  }

  async function clone() {
    setCloning(true);
    setError(null);
    try {
      await cloneWorkflow(id);
      await load(); // same id now resolves to the new editable draft version
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setCloning(false);
    }
  }

  const editing = nodes.find((n) => n.id === editingId);
  const readOnly = status === "published";

  return (
    <div className="space-y-4">
      <div>
        <Link href="/studio" className="text-sm text-muted hover:underline">
          ← Studio
        </Link>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-3">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={readOnly}
            aria-label="Workflow name"
            className="-ml-2 min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1 text-2xl font-semibold tracking-tight hover:border-border focus-visible:border-accent disabled:opacity-70"
          />
          {readOnly ? (
            <Button variant="primary" onClick={clone} disabled={cloning}>
              {cloning ? "Cloning…" : "Clone to draft"}
            </Button>
          ) : (
            <Button variant="primary" onClick={save} disabled={saveState === "saving"}>
              {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Saved ✓" : "Save"}
            </Button>
          )}
        </div>
      </div>

      {error && <ErrorBox message={error} />}

      {/* Amber is reserved for the read-only warning; routine draft editing gets a
          neutral note so the banner still means something when it matters. */}
      <div
        className={
          readOnly
            ? "rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-300"
            : "rounded-lg border border-border bg-surface px-3 py-2 text-xs text-muted"
        }
      >
        {readOnly ? (
          <>
            This workflow is <strong>published</strong> and read-only. Clone it to a draft
            to edit its structure.
          </>
        ) : (
          <>
            Editing a <strong>draft</strong>: nodes, edges, config, and layout save to this
            draft. Fields marked <span className="font-semibold">*</span> are required for the
            graph to publish. Publishing &amp; running additionally need contracts, a
            registered runner, and a deployment (the medium-code path).
          </>
        )}
      </div>

      <div className="flex flex-col gap-4 lg:flex-row">
        <aside className="w-full shrink-0 lg:w-60">
          <Card title="Add node">
            <div className="space-y-1.5">
              {palette.map((t) => (
                <button
                  key={t.type}
                  onClick={() => addNode(t)}
                  disabled={readOnly}
                  title={readOnly ? "Clone to a draft to edit" : `Add ${t.label}`}
                  className="group flex w-full items-center gap-3 rounded-lg border border-transparent px-2.5 py-2 text-left transition-colors hover:border-border hover:bg-accent/[0.04] disabled:opacity-50 disabled:hover:border-transparent disabled:hover:bg-transparent"
                >
                  <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-accent/10 text-accent">
                    <NodeGlyph type={t.type} className="h-4 w-4" />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm font-medium">{t.label}</span>
                    <span className="block truncate text-xs text-muted">
                      {NODE_META[t.type]?.blurb ?? t.category}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </Card>
          <p className="mt-2 px-1 text-xs text-muted">
            Click a node to edit it. Select a node or edge and press{" "}
            <kbd className="rounded border border-border bg-surface px-1">Backspace</kbd> to
            delete.
          </p>
        </aside>

        <div
          ref={paneRef}
          className="h-[60vh] flex-1 overflow-hidden rounded-xl border border-border bg-surface lg:h-[70vh]"
        >
          {loading ? (
            <div className="flex h-full items-center justify-center text-sm text-muted">
              Loading graph…
            </div>
          ) : (
            <ReactFlow
              aria-label="Workflow graph editor"
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              nodesFocusable
              deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onInit={setRf}
              onNodeClick={(_, node) => setEditingId(node.id)}
              fitView
              fitViewOptions={{ maxZoom: 1, padding: 0.25 }}
              minZoom={0.3}
              defaultEdgeOptions={{ markerEnd: { type: MarkerType.ArrowClosed } }}
              proOptions={{ hideAttribution: true }}
            >
              <Background />
              <Controls />
              <MiniMap pannable zoomable />
              {nodes.length === 0 && (
                <Panel position="top-center">
                  <div className="mt-12 rounded-lg border border-dashed border-border bg-surface/80 px-4 py-3 text-center text-sm text-muted">
                    Add a node from the palette to start building.
                  </div>
                </Panel>
              )}
            </ReactFlow>
          )}
        </div>
      </div>

      {editing && (
        <NodeEditorDialog
          node={editing}
          readOnly={readOnly}
          onClose={() => setEditingId(null)}
          onPatch={(patch) => patchNode(editing.id, patch)}
          onDelete={() => deleteNode(editing.id)}
        />
      )}
    </div>
  );
}

function NodeEditorDialog({
  node,
  readOnly,
  onClose,
  onPatch,
  onDelete,
}: {
  node: Node;
  readOnly: boolean;
  onClose: () => void;
  onPatch: (patch: Partial<{ label: string; config: Cfg }>) => void;
  onDelete: () => void;
}) {
  const d = node.data as { studioType: string; label: string; config: Cfg };
  const closeRef = useRef<HTMLButtonElement>(null);

  // Move focus into the dialog on open so keyboard users aren't left behind
  // on the canvas.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-20"
      onMouseDown={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Edit ${d.label}`}
        className="w-full max-w-md rounded-xl border border-border bg-surface shadow-xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-accent/10 text-accent">
              <NodeGlyph type={d.studioType} className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">{d.label || d.studioType}</div>
              <div className="text-[11px] uppercase tracking-wide text-muted">{d.studioType}</div>
            </div>
          </div>
          <Button ref={closeRef} variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </header>

        <div className="max-h-[60vh] overflow-auto p-4">
          <NodeInspector
            studioType={d.studioType}
            label={d.label}
            config={d.config}
            readOnly={readOnly}
            onLabelChange={(label) => onPatch({ label })}
            onConfigChange={(config) => onPatch({ config })}
          />
        </div>

        <footer className="flex items-center justify-between border-t border-border px-4 py-3">
          {readOnly ? (
            <span className="text-xs text-muted">Read-only (published)</span>
          ) : (
            <Button variant="danger" size="sm" onClick={onDelete}>
              Delete node
            </Button>
          )}
          <Button variant="primary" size="sm" onClick={onClose}>
            Done
          </Button>
        </footer>
      </div>
    </div>
  );
}
