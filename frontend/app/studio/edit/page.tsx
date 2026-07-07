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
import { Button, ErrorBox } from "@/app/components/ui";
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
import { WORKFLOW_TEMPLATES } from "@/app/lib/templates";

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

  // Populate the empty canvas with a small working example (the RAG template)
  // so a fresh draft never has to start from nothing.
  const insertExample = useCallback(() => {
    const t = WORKFLOW_TEMPLATES[0];
    setNodes(
      t.nodes.map((n) => {
        const d = (n.data ?? {}) as { label?: string; config?: Cfg };
        return {
          id: n.id,
          type: "studio",
          position: { x: n.position.x, y: n.position.y },
          data: {
            label: d.label ?? n.id,
            studioType: n.type,
            ports: portsFor(n.type, palette),
            config: d.config ?? {},
          },
        };
      }),
    );
    setEdges(
      t.edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.source_handle ?? undefined,
        targetHandle: e.target_handle ?? undefined,
      })),
    );
    window.setTimeout(() => rf?.fitView({ maxZoom: 1, padding: 0.25 }), 0);
  }, [palette, rf, setNodes, setEdges]);

  // One-click cleanup: topological left-to-right layout (roots on the left,
  // each node one column right of its furthest predecessor), columns centered
  // vertically, then re-center the viewport on the result.
  const tidyLayout = useCallback(() => {
    setNodes((ns) => {
      if (ns.length < 2) return ns;
      const level = new Map<string, number>(ns.map((n) => [n.id, 0]));
      // Longest-path relaxation; the pass cap keeps cycles from looping forever.
      for (let pass = 0; pass < ns.length; pass++) {
        let changed = false;
        for (const e of edges) {
          const next = (level.get(e.source) ?? 0) + 1;
          if (next > (level.get(e.target) ?? 0)) {
            level.set(e.target, next);
            changed = true;
          }
        }
        if (!changed) break;
      }
      const columns = new Map<number, string[]>();
      ns.forEach((n) => {
        const l = level.get(n.id) ?? 0;
        columns.set(l, [...(columns.get(l) ?? []), n.id]);
      });
      const pos = new Map<string, { x: number; y: number }>();
      columns.forEach((ids, l) => {
        ids.forEach((id, i) => {
          pos.set(id, { x: l * 280, y: (i - (ids.length - 1) / 2) * 120 });
        });
      });
      return ns.map((n) => ({ ...n, position: pos.get(n.id) ?? n.position }));
    });
    window.setTimeout(() => rf?.fitView({ maxZoom: 1, padding: 0.25 }), 0);
  }, [edges, rf, setNodes]);

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

  // Full-bleed canvas (n8n-style): the editor escapes the centered page
  // column and fills the viewport below the sticky h-14 header; title,
  // palette, and actions float over the graph as panels.
  return (
    <div ref={paneRef} className="fixed inset-x-0 bottom-0 top-14">
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

          {/* Floating title + palette */}
          <Panel position="top-left">
            <div className="w-72 space-y-2">
              <div className="rounded-xl border border-border bg-surface p-3 shadow-md shadow-black/[0.06]">
                <Link href="/studio" className="text-xs text-muted hover:underline">
                  ← Studio
                </Link>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={readOnly}
                  aria-label="Workflow name"
                  className="-ml-1.5 mt-0.5 w-full rounded-lg border border-transparent bg-transparent px-1.5 py-0.5 text-lg font-semibold tracking-tight hover:border-border focus-visible:border-accent disabled:opacity-70"
                />
                {/* Amber is reserved for the read-only warning; routine draft
                    editing gets a neutral note so it means something when it matters. */}
                {readOnly ? (
                  <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">
                    <strong>Published</strong> &amp; read-only — clone to a draft to edit.
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-muted">
                    Draft — nodes, edges, config &amp; layout save here. Fields marked{" "}
                    <span className="font-semibold">*</span> are required to publish.
                  </p>
                )}
              </div>

              <div className="rounded-xl border border-border bg-surface shadow-md shadow-black/[0.06]">
                <div className="border-b border-border px-3 py-2 text-sm font-semibold">
                  Add node
                </div>
                <div className="max-h-[42vh] space-y-1 overflow-y-auto p-2">
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
                        <span className="line-clamp-2 block text-xs text-muted">
                          {NODE_META[t.type]?.blurb ?? t.category}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>

              <p className="px-1 text-xs text-muted">
                Click a node to edit it.{" "}
                <kbd className="rounded border border-border bg-surface px-1">Backspace</kbd>{" "}
                deletes the selection.
              </p>
            </div>
          </Panel>

          {/* Floating actions */}
          <Panel position="top-right">
            <div className="flex flex-col items-end gap-2">
              <div className="flex items-center gap-2">
                {!readOnly && nodes.length > 1 && (
                  <Button size="sm" onClick={tidyLayout} title="Auto-arrange and center the graph">
                    Tidy layout
                  </Button>
                )}
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
              {error && (
                <div className="max-w-sm">
                  <ErrorBox message={error} />
                </div>
              )}
            </div>
          </Panel>

          {nodes.length === 0 && (
            <Panel position="top-center">
              <div className="mt-12 rounded-lg border border-dashed border-border bg-surface/80 px-4 py-3 text-center text-sm text-muted">
                Add a node from the palette to start building.
                {!readOnly && (
                  <div className="mt-2">
                    <Button size="sm" onClick={insertExample}>
                      Insert example graph
                    </Button>
                  </div>
                )}
              </div>
            </Panel>
          )}
        </ReactFlow>
      )}

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
