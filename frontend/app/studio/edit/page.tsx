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
import { StudioNodeView, type Port } from "@/app/components/StudioNodeView";
import { Button, ErrorBox } from "@/app/components/ui";
import {
  errMsg,
  getWorkflow,
  listNodeTypes,
  updateWorkflow,
  type NodeType,
  type StudioEdge,
  type StudioNode,
  type WorkflowDetail,
} from "@/app/lib/api";

// Stable reference — required by React Flow.
const nodeTypes = { studio: StudioNodeView };

function portsFor(type: string, types: NodeType[]): Port[] {
  return (types.find((t) => t.type === type)?.ports ?? []) as Port[];
}

function toRfNodes(detail: WorkflowDetail, types: NodeType[]): Node[] {
  return detail.nodes.map((n) => ({
    id: n.id,
    type: "studio",
    position: { x: n.position.x, y: n.position.y },
    data: {
      label: (n.data?.label as string) || n.id,
      studioType: n.type,
      ports: portsFor(n.type, types),
    },
  }));
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
  return nodes.map((n) => ({
    id: n.id,
    type: (n.data as { studioType: string }).studioType,
    position: { x: n.position.x, y: n.position.y },
    data: { label: (n.data as { label: string }).label },
  }));
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

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [detail, types] = await Promise.all([getWorkflow(id), listNodeTypes()]);
        if (!alive) return;
        setName(detail.name);
        setStatus(detail.status);
        setPalette(types);
        setNodes(toRfNodes(detail, types));
        setEdges(toRfEdges(detail));
      } catch (e) {
        if (alive) setError(errMsg(e));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [id, setNodes, setEdges]);

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
          data: { label: t.label, studioType: t.type, ports: t.ports },
        }),
      );
    },
    [setNodes],
  );

  const renameSelected = useCallback(
    (label: string) => {
      if (!selectedId) return;
      setNodes((ns) =>
        ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, label } } : n)),
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

  const selected = nodes.find((n) => n.id === selectedId);
  const readOnly = status === "published";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Link href="/studio" className="text-sm text-zinc-500 hover:underline">
            ← Studio
          </Link>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium dark:border-zinc-700 dark:bg-zinc-900"
          />
        </div>
        <Button
          variant="primary"
          onClick={save}
          disabled={saveState === "saving" || readOnly}
          title={readOnly ? "Published graphs are read-only" : undefined}
        >
          {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Saved ✓" : "Save"}
        </Button>
      </div>

      {error && <ErrorBox message={error} />}

      <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
        {readOnly ? (
          <>
            This workflow is <strong>published</strong> and read-only. Saving is
            disabled — clone it to a draft to edit.
          </>
        ) : (
          <>
            Layout (node positions) and viewport are saved. Creating or removing
            executable nodes and edges isn&apos;t persisted by the studio API yet —
            those edits are visual-only for now.
          </>
        )}
      </div>

      <div className="flex gap-4">
        {/* Palette */}
        <aside className="w-44 shrink-0 space-y-3">
          <div className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
            Add node
          </div>
          <div className="space-y-1.5">
            {palette.map((t) => (
              <button
                key={t.type}
                onClick={() => addNode(t)}
                className="w-full rounded-md border border-zinc-300 px-3 py-1.5 text-left text-sm hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
              >
                <div className="font-medium">{t.label}</div>
                <div className="text-[10px] text-zinc-400">{t.category}</div>
              </button>
            ))}
          </div>

          {selected && (
            <div className="space-y-1 border-t border-zinc-200 pt-3 dark:border-zinc-800">
              <div className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                Selected node
              </div>
              <label className="block text-xs text-zinc-500">Label</label>
              <input
                value={(selected.data as { label: string }).label}
                onChange={(e) => renameSelected(e.target.value)}
                className="w-full rounded-md border border-zinc-300 px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-900"
              />
              <p className="text-[10px] text-zinc-400">
                Select an edge/node and press Backspace to delete.
              </p>
            </div>
          )}
        </aside>

        {/* Canvas */}
        <div className="h-[70vh] flex-1 overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
          {loading ? (
            <div className="flex h-full items-center justify-center text-sm text-zinc-400">
              Loading graph…
            </div>
          ) : (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
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
