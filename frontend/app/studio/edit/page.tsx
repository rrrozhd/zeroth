"use client";

import "@xyflow/react/dist/style.css";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  addEdge,
  Background,
  Controls,
  MiniMap,
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
  const [selectedId, setSelectedId] = useState<string | null>(null);

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
      setNodes((ns) =>
        ns.concat({
          id: newId,
          type: "studio",
          position: { x: 80 + ns.length * 30, y: 80 + ns.length * 30 },
          data: {
            label: t.label,
            studioType: t.type,
            ports: t.ports,
            config: { ...(DEFAULT_CONFIG[t.type] ?? {}) },
          },
        }),
      );
    },
    [setNodes],
  );

  const patchSelected = useCallback(
    (patch: Partial<{ label: string; config: Cfg }>) => {
      if (!selectedId) return;
      setNodes((ns) =>
        ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, ...patch } } : n)),
      );
    },
    [selectedId, setNodes],
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

  const selected = nodes.find((n) => n.id === selectedId);
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

      <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-300">
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
        <aside className="w-full shrink-0 space-y-3 lg:w-56">
          <Card title="Add node">
            <div className="space-y-1.5">
              {palette.map((t) => (
                <button
                  key={t.type}
                  onClick={() => addNode(t)}
                  disabled={readOnly}
                  className="w-full rounded-lg border border-border px-3 py-1.5 text-left text-sm transition-colors hover:border-accent/40 hover:bg-accent/[0.04] disabled:opacity-50"
                >
                  <div className="font-medium">{t.label}</div>
                  <div className="text-[10px] text-muted">{t.type}</div>
                </button>
              ))}
            </div>
          </Card>

          {selected && (
            <Card title="Inspector">
              <NodeInspector
                studioType={(selected.data as { studioType: string }).studioType}
                label={(selected.data as { label: string }).label}
                config={(selected.data as { config: Cfg }).config}
                onLabelChange={(label) => patchSelected({ label })}
                onConfigChange={(config) => patchSelected({ config })}
              />
            </Card>
          )}
        </aside>

        <div className="h-[60vh] flex-1 overflow-hidden rounded-xl border border-border bg-surface lg:h-[70vh]">
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
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onInit={setRf}
              onSelectionChange={({ nodes: sel }) =>
                setSelectedId(sel.length === 1 ? sel[0].id : null)
              }
              fitView
              proOptions={{ hideAttribution: true }}
            >
              <Background />
              <Controls />
              <MiniMap pannable zoomable />
            </ReactFlow>
          )}
        </div>
      </div>
    </div>
  );
}
