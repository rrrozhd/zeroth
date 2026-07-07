"use client";

import "@xyflow/react/dist/style.css";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { Button, ErrorBox, StatusBadge } from "@/app/components/ui";
import {
  ApiError,
  cloneWorkflow,
  errMsg,
  getWorkflow,
  listConnectors,
  listNodeTypes,
  listWorkflows,
  updateWorkflow,
  type NodeType,
  type StudioEdge,
  type StudioNode,
  type WorkflowDetail,
  type WorkflowSummary,
} from "@/app/lib/api";
import { setLastWorkflowId } from "@/app/lib/lastWorkflow";
import { WORKFLOW_TEMPLATES } from "@/app/lib/templates";

const nodeTypes = { studio: StudioNodeView };

type Cfg = Record<string, unknown>;

function portsFor(type: string, types: NodeType[]): Port[] {
  return (types.find((t) => t.type === type)?.ports ?? []) as Port[];
}

// Structural signature for autosave + undo history. Selection lives on the
// node objects (`selected`), so key off ids/positions/data only — clicking a
// node must not schedule a save or record a history entry.
function graphSig(nodes: Node[], edges: Edge[]): string {
  return JSON.stringify([
    nodes.map((n) => [n.id, n.position.x, n.position.y, n.data]),
    edges.map((e) => [e.id, e.source, e.sourceHandle ?? null, e.target, e.targetHandle ?? null]),
  ]);
}

type Snapshot = { nodes: Node[]; edges: Edge[] };

const HISTORY_CAP = 50;

// Cross-workflow node clipboard (n8n-style): localStorage survives navigation,
// so nodes copied here can be pasted into another workflow's editor.
const CLIPBOARD_KEY = "zeroth.studio.clipboard";

type ClipboardPayload = {
  nodes: {
    id: string;
    studioType: string;
    label: string;
    config: Cfg;
    position: { x: number; y: number };
  }[];
  edges: {
    id: string;
    source: string;
    target: string;
    sourceHandle: string | null;
    targetHandle: string | null;
  }[];
  ts: number;
};

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
  // Static export requires a Suspense boundary around useSearchParams; keeping
  // the hook in a child also lets client-side ?id= changes (the quick-switcher)
  // reach the keyed editor below.
  return (
    <Suspense fallback={null}>
      <StudioEditQuery />
    </Suspense>
  );
}

function StudioEditQuery() {
  const id = useSearchParams().get("id");

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

  // Keyed by id: switching workflows client-side remounts the editor (and its
  // React Flow store) so load() runs fresh for the new graph.
  return (
    <ReactFlowProvider key={id}>
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
  const [saveState, setSaveState] = useState<"idle" | "dirty" | "saving" | "saved">("idle");
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [cloning, setCloning] = useState(false);
  const [copied, setCopied] = useState<number | null>(null);
  const [histState, setHistState] = useState({ canUndo: false, canRedo: false });
  // Autosave/history bookkeeping. loadedRef gates both until the first
  // successful load; the sig refs suppress feedback loops (autosaving what was
  // just loaded/saved, re-recording an applied undo as a fresh edit).
  const loadedRef = useRef(false);
  const lastSavedSigRef = useRef("");
  const lastHistSigRef = useRef("");
  const saveInFlightRef = useRef(false);
  const savePendingRef = useRef(false);
  const saveNowRef = useRef<() => Promise<void>>(async () => {});
  const histRef = useRef<{ stack: Snapshot[]; index: number }>({ stack: [], index: -1 });
  const pasteSeqRef = useRef(0);
  // Registered memory connectors — feeds the retrieval node's connector
  // dropdown. Non-fatal if unavailable (the field degrades to a text input).
  const [connectorRefs, setConnectorRefs] = useState<string[]>([]);
  // Other workflows in this deployment — feeds the quick-switcher card.
  // Non-fatal if unavailable (the card just doesn't render).
  const [others, setOthers] = useState<WorkflowSummary[]>([]);
  const [switcherOpen, setSwitcherOpen] = useState(false);

  const readOnly = status === "published";
  const sig = useMemo(() => graphSig(nodes, edges), [nodes, edges]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [detail, types, connectors, all] = await Promise.all([
        getWorkflow(id),
        listNodeTypes(),
        listConnectors().catch(() => []),
        listWorkflows().catch(() => []),
      ]);
      setName(detail.name);
      setStatus(detail.status);
      setPalette(types);
      setConnectorRefs(connectors.map((c) => c.ref));
      setOthers(all.filter((w) => w.id !== id));
      const rfNodes = toRfNodes(detail, types);
      const rfEdges = toRfEdges(detail);
      setNodes(rfNodes);
      setEdges(rfEdges);
      setLastWorkflowId(id);
      // Baseline for autosave + undo: the freshly loaded graph counts as saved
      // and is history entry 0 (clone() re-loads, so this also resets both).
      lastSavedSigRef.current = JSON.stringify([graphSig(rfNodes, rfEdges), detail.name]);
      lastHistSigRef.current = graphSig(rfNodes, rfEdges);
      histRef.current = { stack: [{ nodes: rfNodes, edges: rfEdges }], index: 0 };
      setHistState({ canUndo: false, canRedo: false });
      loadedRef.current = true;
    } catch (e) {
      // A stored id that 404s must not keep steering the Studio nav link back
      // to a dead editor page.
      if (e instanceof ApiError && e.status === 404) setLastWorkflowId(null);
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

  const saveNow = useCallback(async () => {
    // Collapse concurrent saves: if one is in flight, remember to run again
    // with the latest state once it settles.
    if (saveInFlightRef.current) {
      savePendingRef.current = true;
      return;
    }
    saveInFlightRef.current = true;
    setSaveState("saving");
    setError(null);
    try {
      await updateWorkflow(id, {
        name,
        nodes: toStudioNodes(nodes),
        edges: toStudioEdges(edges),
        viewport: rf ? rf.getViewport() : { x: 0, y: 0, zoom: 1 },
      });
      lastSavedSigRef.current = JSON.stringify([graphSig(nodes, edges), name]);
      setSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      setSaveState("saved");
    } catch (e) {
      setError(errMsg(e));
      setSaveState("dirty");
    } finally {
      saveInFlightRef.current = false;
      if (savePendingRef.current) {
        savePendingRef.current = false;
        window.setTimeout(() => void saveNowRef.current(), 0);
      }
    }
  }, [id, name, nodes, edges, rf]);
  saveNowRef.current = saveNow;

  // Debounced autosave (drafts only): any structural or name change after the
  // initial load schedules a save 1.5s after the last change.
  useEffect(() => {
    if (!loadedRef.current || readOnly) return;
    if (JSON.stringify([sig, name]) === lastSavedSigRef.current) return;
    setSaveState("dirty");
    const t = window.setTimeout(() => void saveNowRef.current(), 1500);
    return () => window.clearTimeout(t);
  }, [sig, name, readOnly]);

  // Debounced history push: drags collapse into one entry. Undo/redo set
  // lastHistSigRef themselves, so applying a snapshot is never re-recorded.
  useEffect(() => {
    if (!loadedRef.current || sig === lastHistSigRef.current) return;
    const t = window.setTimeout(() => {
      const h = histRef.current;
      const stack = h.stack.slice(0, h.index + 1);
      stack.push({ nodes, edges });
      if (stack.length > HISTORY_CAP) stack.shift();
      histRef.current = { stack, index: stack.length - 1 };
      lastHistSigRef.current = sig;
      setHistState({ canUndo: stack.length > 1, canRedo: false });
    }, 300);
    return () => window.clearTimeout(t);
  }, [sig, nodes, edges]);

  const undo = useCallback(() => {
    const h = histRef.current;
    if (h.index <= 0) return;
    h.index -= 1;
    const snap = h.stack[h.index];
    lastHistSigRef.current = graphSig(snap.nodes, snap.edges);
    setNodes(snap.nodes);
    setEdges(snap.edges);
    setHistState({ canUndo: h.index > 0, canRedo: true });
  }, [setNodes, setEdges]);

  const redo = useCallback(() => {
    const h = histRef.current;
    if (h.index >= h.stack.length - 1) return;
    h.index += 1;
    const snap = h.stack[h.index];
    lastHistSigRef.current = graphSig(snap.nodes, snap.edges);
    setNodes(snap.nodes);
    setEdges(snap.edges);
    setHistState({ canUndo: h.index > 0, canRedo: h.index < h.stack.length - 1 });
  }, [setNodes, setEdges]);

  const copySelection = useCallback(() => {
    const sel = nodes.filter((n) => n.selected);
    if (sel.length === 0) return;
    const ids = new Set(sel.map((n) => n.id));
    const payload: ClipboardPayload = {
      nodes: sel.map((n) => {
        const d = n.data as { studioType: string; label: string; config: Cfg };
        return {
          id: n.id,
          studioType: d.studioType,
          label: d.label,
          config: d.config,
          position: { x: n.position.x, y: n.position.y },
        };
      }),
      edges: edges
        .filter((e) => ids.has(e.source) && ids.has(e.target))
        .map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          sourceHandle: e.sourceHandle ?? null,
          targetHandle: e.targetHandle ?? null,
        })),
      ts: Date.now(),
    };
    try {
      window.localStorage.setItem(CLIPBOARD_KEY, JSON.stringify(payload));
      setCopied(sel.length);
    } catch {
      // Storage full or denied — copy is best-effort.
    }
  }, [nodes, edges]);

  useEffect(() => {
    if (copied === null) return;
    const t = window.setTimeout(() => setCopied(null), 2000);
    return () => window.clearTimeout(t);
  }, [copied]);

  const pasteClipboard = useCallback(() => {
    if (readOnly) return;
    let clip: ClipboardPayload | null = null;
    try {
      clip = JSON.parse(window.localStorage.getItem(CLIPBOARD_KEY) ?? "null");
    } catch {
      return; // stale/corrupt clipboard — ignore
    }
    if (!clip || !Array.isArray(clip.nodes)) return;
    const src = clip.nodes.filter(
      (n) =>
        n &&
        typeof n.id === "string" &&
        typeof n.studioType === "string" &&
        typeof n.position?.x === "number" &&
        typeof n.position?.y === "number",
    );
    if (src.length === 0) return;
    // Stamp is unique per paste even within the same millisecond.
    const stamp = `${Date.now().toString(36)}${(pasteSeqRef.current++).toString(36)}`;
    const idMap = new Map<string, string>();
    for (const n of src) idMap.set(n.id, `${n.id}-copy-${stamp}`);
    const pasted: Node[] = src.map((n) => ({
      id: idMap.get(n.id)!,
      type: "studio",
      // n8n-like: offset +32/+32 from the originally copied positions.
      position: { x: n.position.x + 32, y: n.position.y + 32 },
      selected: true,
      data: {
        label: n.label ?? n.id,
        studioType: n.studioType,
        ports: portsFor(n.studioType, palette),
        config: { ...(n.config ?? {}) },
      },
    }));
    const pastedEdges: Edge[] = (Array.isArray(clip.edges) ? clip.edges : [])
      .filter((e) => e && idMap.has(e.source) && idMap.has(e.target))
      .map((e) => ({
        id: `${e.id}-copy-${stamp}`,
        source: idMap.get(e.source)!,
        target: idMap.get(e.target)!,
        sourceHandle: e.sourceHandle ?? undefined,
        targetHandle: e.targetHandle ?? undefined,
      }));
    // Pasted nodes become the selection so they can be dragged right away.
    setNodes((ns) => ns.map((n) => (n.selected ? { ...n, selected: false } : n)).concat(pasted));
    setEdges((es) => es.concat(pastedEdges));
  }, [readOnly, palette, setNodes, setEdges]);

  // Document-level shortcuts. React Flow's deleteKeyCode only claims
  // Backspace/Delete, so there is no overlap; the focus guard leaves native
  // text editing (incl. its own undo) alone inside form fields.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      )
        return;
      if (!(e.metaKey || e.ctrlKey)) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) {
        if (readOnly) return;
        e.preventDefault();
        undo();
      } else if ((k === "z" && e.shiftKey) || k === "y") {
        if (readOnly) return;
        e.preventDefault();
        redo();
      } else if (k === "c") {
        copySelection(); // allowed read-only: copy out of a published graph
      } else if (k === "v") {
        if (!readOnly) pasteClipboard();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [readOnly, undo, redo, copySelection, pasteClipboard]);

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
                {/* Clearing the remembered id here keeps the list reachable:
                    the nav's Studio link otherwise points back at this editor. */}
                <Link
                  href="/studio"
                  onClick={() => setLastWorkflowId(null)}
                  className="text-xs text-muted hover:underline"
                >
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

              {others.length > 0 && (
                <div className="rounded-xl border border-border bg-surface shadow-md shadow-black/[0.06]">
                  <button
                    onClick={() => setSwitcherOpen((o) => !o)}
                    aria-expanded={switcherOpen}
                    className="flex w-full items-center justify-between px-3 py-2 text-sm font-semibold"
                  >
                    Workflows ({others.length})
                    <span
                      aria-hidden
                      className={`text-xs text-muted transition-transform ${switcherOpen ? "rotate-180" : ""}`}
                    >
                      ▾
                    </span>
                  </button>
                  {switcherOpen && (
                    <div className="max-h-40 space-y-0.5 overflow-y-auto border-t border-border p-1.5">
                      {others.map((w) => (
                        <Link
                          key={w.id}
                          href={`/studio/edit?id=${encodeURIComponent(w.id)}`}
                          className="flex items-center justify-between gap-2 rounded-lg border border-transparent px-2 py-1.5 text-sm transition-colors hover:border-border hover:bg-accent/[0.04]"
                        >
                          <span className="min-w-0 truncate">{w.name}</span>
                          <span className="shrink-0">
                            <StatusBadge status={w.status} />
                          </span>
                        </Link>
                      ))}
                    </div>
                  )}
                </div>
              )}

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
                {!readOnly && (
                  <span aria-live="polite" className="text-xs text-muted">
                    {saveState === "saving"
                      ? "Saving…"
                      : saveState === "dirty"
                        ? "Unsaved changes"
                        : savedAt
                          ? `Saved ${savedAt}`
                          : ""}
                  </span>
                )}
                {!readOnly && (
                  <>
                    <Button
                      size="sm"
                      onClick={undo}
                      disabled={!histState.canUndo}
                      aria-label="Undo"
                      title="Undo (Ctrl/⌘+Z)"
                    >
                      ↶
                    </Button>
                    <Button
                      size="sm"
                      onClick={redo}
                      disabled={!histState.canRedo}
                      aria-label="Redo"
                      title="Redo (Ctrl/⌘+Shift+Z)"
                    >
                      ↷
                    </Button>
                  </>
                )}
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
                  <Button variant="primary" onClick={saveNow} disabled={saveState === "saving"}>
                    Save
                  </Button>
                )}
              </div>
              {copied !== null && (
                <span className="text-xs text-muted">
                  Copied {copied} node{copied === 1 ? "" : "s"}
                </span>
              )}
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
          connectorRefs={connectorRefs}
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
  connectorRefs,
  onClose,
  onPatch,
  onDelete,
}: {
  node: Node;
  readOnly: boolean;
  connectorRefs: string[];
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
            dynamicOptions={{ connectors: connectorRefs }}
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
