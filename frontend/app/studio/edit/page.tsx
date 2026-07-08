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
import {
  deriveNodeStates,
  entryPhase,
  PublishIssuesContext,
  RunStateContext,
  type NodeRunState,
} from "@/app/components/runState";
import { StudioNodeView, type Port } from "@/app/components/StudioNodeView";
import {
  ApiErrorNote,
  Button,
  Empty,
  ErrorBox,
  Field,
  fmtTime,
  fmtUsd,
  buildRunCurl,
  CurlBlock,
  Input,
  Json,
  Mono,
  Skeleton,
  StatusBadge,
  Textarea,
} from "@/app/components/ui";
import {
  ApiError,
  cloneWorkflow,
  createDeployment,
  diffWorkflow,
  errMsg,
  getHealth,
  getRun,
  getRunTimeline,
  getWorkflow,
  listConnectors,
  listContracts,
  listManifests,
  listNodeAudits,
  listNodeTypes,
  listRuns,
  listWorkflows,
  publishIssuesOf,
  publishWorkflow,
  submitRun,
  updateWorkflow,
  type ConnectorSummary,
  type DeploymentSummary,
  type DiffEntry,
  type NodeAuditRecord,
  type NodeType,
  type PublishIssue,
  type RunStatus,
  type StudioEdge,
  type StudioNode,
  type WorkflowDetail,
  type WorkflowDiff,
  type WorkflowSummary,
} from "@/app/lib/api";
import { getApiKey } from "@/app/lib/config";
import { setLastWorkflowId } from "@/app/lib/lastWorkflow";
import { WORKFLOW_TEMPLATES } from "@/app/lib/templates";

const nodeTypes = { studio: StudioNodeView };

type Cfg = Record<string, unknown>;

// Editable slice of a canvas node's data (inspector writes through patchNode).
type NodePatch = Partial<{
  label: string;
  config: Cfg;
  inputContractRef: string | null;
  outputContractRef: string | null;
}>;

function portsFor(type: string, types: NodeType[]): Port[] {
  return (types.find((t) => t.type === type)?.ports ?? []) as Port[];
}

// Tool edges (agent Tools handle -> unit Tool handle) are a separate set of
// edges from the data flow: violet and dashed on the canvas, kind="tool" in
// the graph. The handle ids are the ground truth for which set an edge is in.
type EdgeKind = "data" | "tool";

const TOOL_EDGE_STYLE = { stroke: "#8b5cf6", strokeDasharray: "6 3" };

function edgeKindOf(e: {
  sourceHandle?: string | null;
  data?: Record<string, unknown>;
}): EdgeKind {
  if ((e.data as { kind?: string } | undefined)?.kind === "tool") return "tool";
  return e.sourceHandle === "tools" ? "tool" : "data";
}

/** Canvas props (styling + kind marker) for an edge of the given kind. */
function edgeKindProps(kind: EdgeKind): Partial<Edge> {
  return kind === "tool" ? { data: { kind }, style: TOOL_EDGE_STYLE } : { data: { kind } };
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
    inputContractRef?: string | null;
    outputContractRef?: string | null;
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
    const data = (n.data ?? {}) as {
      label?: string;
      config?: Cfg;
      input_contract_ref?: string | null;
      output_contract_ref?: string | null;
    };
    return {
      id: n.id,
      type: "studio",
      position: { x: n.position.x, y: n.position.y },
      data: {
        label: data.label || n.id,
        studioType: n.type,
        ports: portsFor(n.type, types),
        config: data.config ?? {},
        // Node-level contract bindings must round-trip — dropping them here
        // would silently strip contracts from graphs authored in Python.
        inputContractRef: data.input_contract_ref ?? null,
        outputContractRef: data.output_contract_ref ?? null,
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
    ...edgeKindProps(e.kind ?? edgeKindOf({ sourceHandle: e.source_handle })),
  }));
}

function toStudioNodes(nodes: Node[]): StudioNode[] {
  return nodes.map((n) => {
    const d = n.data as {
      studioType: string;
      label: string;
      config: Cfg;
      inputContractRef?: string | null;
      outputContractRef?: string | null;
    };
    return {
      id: n.id,
      type: d.studioType,
      position: { x: n.position.x, y: n.position.y },
      data: {
        label: d.label,
        config: d.config,
        input_contract_ref: d.inputContractRef ?? null,
        output_contract_ref: d.outputContractRef ?? null,
      },
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
    kind: edgeKindOf(e),
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
  // The graph's entrypoint node id ("" = unset). Required to publish; saved
  // through PUT alongside the structure ("" clears it server-side).
  const [entryStep, setEntryStep] = useState("");
  const [version, setVersion] = useState(1);
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
  // dropdown and its inline settings panel. Non-fatal if unavailable (the
  // field degrades to a text input).
  const [connectors, setConnectors] = useState<ConnectorSummary[]>([]);
  // Registered executable-unit manifests — feeds the executable_unit node's
  // manifest_ref dropdown. Same degrade-to-text fallback as connectors.
  const [manifestRefs, setManifestRefs] = useState<string[]>([]);
  // Registered contracts — feeds the inspector's contract-ref pickers.
  const [contractNames, setContractNames] = useState<string[]>([]);
  // Re-fetch after the picker's inline "New contract" registers one.
  const refreshContracts = useCallback(async () => {
    try {
      setContractNames((await listContracts()).map((c) => c.name));
    } catch {
      /* picker keeps its current list */
    }
  }, []);
  // Other workflows in this deployment — feeds the quick-switcher card.
  // Non-fatal if unavailable (the card just doesn't render).
  const [others, setOthers] = useState<WorkflowSummary[]>([]);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  // Run overlay per node id, painted by the RunPanel. Lives in context (not
  // node.data) so runs never dirty the autosave signature or undo history.
  const [runStates, setRunStates] = useState<Record<string, NodeRunState>>({});
  // Structured issue list from the last failed publish (422); same context
  // rule as run state — never stored in node.data.
  const [publishIssues, setPublishIssues] = useState<PublishIssue[] | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [justPublished, setJustPublished] = useState(false);
  const [deployOpen, setDeployOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);

  const readOnly = status === "published";
  const sig = useMemo(() => graphSig(nodes, edges), [nodes, edges]);
  const nodeIds = useMemo(() => nodes.map((n) => n.id), [nodes]);
  // With an Entrypoint node on the canvas, entry_step derives from it — the
  // manual selector only remains for legacy drafts authored before it existed.
  const hasEntrypointNode = useMemo(
    () => nodes.some((n) => (n.data as { studioType?: string }).studioType === "entrypoint"),
    [nodes],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [detail, types, conns, manifests, contracts, all] = await Promise.all([
        getWorkflow(id),
        listNodeTypes(),
        listConnectors().catch(() => []),
        listManifests().catch(() => []),
        listContracts().catch(() => []),
        listWorkflows().catch(() => []),
      ]);
      setName(detail.name);
      setStatus(detail.status);
      setEntryStep(detail.entry_step ?? "");
      setVersion(detail.version);
      setPalette(types);
      setConnectors(conns);
      setManifestRefs(
        manifests.filter((m) => m.kind === "executable_unit").map((m) => m.manifest_ref),
      );
      setContractNames(contracts.map((c) => c.name));
      setOthers(all.filter((w) => w.id !== id));
      const rfNodes = toRfNodes(detail, types);
      const rfEdges = toRfEdges(detail);
      setNodes(rfNodes);
      setEdges(rfEdges);
      setLastWorkflowId(id);
      // Baseline for autosave + undo: the freshly loaded graph counts as saved
      // and is history entry 0 (clone() re-loads, so this also resets both).
      lastSavedSigRef.current = JSON.stringify([
        graphSig(rfNodes, rfEdges),
        detail.name,
        detail.entry_step ?? "",
      ]);
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

  // Inline connector edits in the node dialog re-fetch the registry; a
  // transient failure keeps the previous list instead of blanking the
  // dropdown mid-edit.
  const reloadConnectors = useCallback(async () => {
    try {
      setConnectors(await listConnectors());
    } catch {
      /* keep the stale list */
    }
  }, []);

  // A tool handle only pairs with a tool handle: the agent's Tools source
  // connects to a unit's Tool target, never to data ports (and vice versa).
  const isValidConnection = useCallback(
    (c: Connection | Edge) => (c.sourceHandle === "tools") === (c.targetHandle === "tool-input"),
    [],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      if ((c.sourceHandle === "tools") !== (c.targetHandle === "tool-input")) return;
      setEdges((es) =>
        addEdge(
          {
            ...c,
            id: `e-${c.source}.${c.sourceHandle}-${c.target}.${c.targetHandle}`,
            ...edgeKindProps(c.sourceHandle === "tools" ? "tool" : "data"),
          },
          es,
        ),
      );
    },
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
            inputContractRef: null,
            outputContractRef: null,
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
            inputContractRef: null,
            outputContractRef: null,
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
    (nodeId: string, patch: NodePatch) => {
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
        // "" clears the entrypoint server-side (null would mean "no change").
        entry_step: entryStep,
        nodes: toStudioNodes(nodes),
        edges: toStudioEdges(edges),
        viewport: rf ? rf.getViewport() : { x: 0, y: 0, zoom: 1 },
      });
      lastSavedSigRef.current = JSON.stringify([graphSig(nodes, edges), name, entryStep]);
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
  }, [id, name, entryStep, nodes, edges, rf]);
  saveNowRef.current = saveNow;

  // Debounced autosave (drafts only): any structural, name, or entrypoint
  // change after the initial load schedules a save 1.5s after the last change.
  useEffect(() => {
    if (!loadedRef.current || readOnly) return;
    if (JSON.stringify([sig, name, entryStep]) === lastSavedSigRef.current) return;
    setSaveState("dirty");
    const t = window.setTimeout(() => void saveNowRef.current(), 1500);
    return () => window.clearTimeout(t);
  }, [sig, name, entryStep, readOnly]);

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
        const d = n.data as {
          studioType: string;
          label: string;
          config: Cfg;
          inputContractRef?: string | null;
          outputContractRef?: string | null;
        };
        return {
          id: n.id,
          studioType: d.studioType,
          label: d.label,
          config: d.config,
          inputContractRef: d.inputContractRef ?? null,
          outputContractRef: d.outputContractRef ?? null,
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
        inputContractRef: n.inputContractRef ?? null,
        outputContractRef: n.outputContractRef ?? null,
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
        ...edgeKindProps(edgeKindOf({ sourceHandle: e.sourceHandle })),
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
      setJustPublished(false);
      await load(); // same id now resolves to the new editable draft version
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setCloning(false);
    }
  }

  // Publish validates against the *saved* draft, so flush the debounced
  // autosave first (including a queued follow-up save) or stale state gets
  // validated.
  async function publish() {
    setPublishing(true);
    setError(null);
    setPublishIssues(null);
    try {
      await saveNowRef.current();
      while (saveInFlightRef.current || savePendingRef.current) {
        await new Promise((r) => window.setTimeout(r, 100));
      }
      await publishWorkflow(id);
      setJustPublished(true);
      await load(); // status flips to published; editor becomes read-only
    } catch (e) {
      const issues = publishIssuesOf(e);
      if (issues) setPublishIssues(issues);
      else setError(errMsg(e));
    } finally {
      setPublishing(false);
    }
  }

  // Jump the canvas to a node referenced by a publish issue.
  const focusNode = useCallback(
    (nodeId: string) => {
      const n = nodes.find((x) => x.id === nodeId);
      if (!n) return;
      setNodes((ns) => ns.map((x) => ({ ...x, selected: x.id === nodeId })));
      // Aim at the node's approximate center (nodes are ~180px wide).
      rf?.setCenter(n.position.x + 90, n.position.y + 24, { zoom: 1.1, duration: 300 });
    },
    [nodes, rf, setNodes],
  );

  // node id -> worst severity, for the canvas issue rings.
  const issueByNode = useMemo(() => {
    const out: Record<string, "error" | "warning"> = {};
    for (const i of publishIssues ?? []) {
      if (!i.node_id) continue;
      if (i.severity === "error" || !out[i.node_id]) {
        out[i.node_id] = i.severity === "error" ? "error" : "warning";
      }
    }
    return out;
  }, [publishIssues]);

  const editing = nodes.find((n) => n.id === editingId);

  // Units attached to the node being edited via tool edges — the agent
  // inspector's "Attached tools" section binds names/descriptions to them.
  const editingToolTargets = useMemo(() => {
    if (!editing) return [];
    return edges
      .filter((e) => e.source === editing.id && edgeKindOf(e) === "tool")
      .map((e) => {
        const target = nodes.find((n) => n.id === e.target);
        return {
          id: e.target,
          label: ((target?.data as { label?: string })?.label ?? e.target) || e.target,
        };
      });
  }, [editing, edges, nodes]);

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
        <RunStateContext.Provider value={runStates}>
        <PublishIssuesContext.Provider value={issueByNode}>
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
          isValidConnection={isValidConnection}
          onInit={setRf}
          // n8n-style: single click only selects (so ⌘C/multi-select feel
          // natural); the config dialog opens on double-click.
          onNodeDoubleClick={(_, node) => setEditingId(node.id)}
          selectionOnDrag
          panOnDrag={[1, 2]}
          panOnScroll
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
                {nodes.length > 0 && !hasEntrypointNode && (
                  <label className="mt-2 block text-xs">
                    <span className="mb-1 flex items-baseline gap-2">
                      <span className="font-medium">
                        Entry step
                        {!readOnly && (
                          <span className="text-red-600 dark:text-red-400"> *</span>
                        )}
                      </span>
                      <span className="font-normal text-muted">where a run starts</span>
                    </span>
                    <select
                      value={entryStep}
                      disabled={readOnly}
                      onChange={(e) => setEntryStep(e.target.value)}
                      className="w-full rounded-lg border border-border bg-surface px-2 py-1 text-xs focus-visible:border-accent disabled:opacity-60"
                    >
                      <option value="">Select…</option>
                      {/* A stale entrypoint (node deleted/renamed) stays selectable
                          so it isn't silently coerced; publish will flag it. */}
                      {entryStep && !nodes.some((n) => n.id === entryStep) && (
                        <option value={entryStep}>{entryStep} (missing)</option>
                      )}
                      {nodes.map((n) => {
                        const d = n.data as { label?: string };
                        return (
                          <option key={n.id} value={n.id}>
                            {d.label && d.label !== n.id ? `${d.label} — ${n.id}` : n.id}
                          </option>
                        );
                      })}
                    </select>
                  </label>
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
                {version > 1 && (
                  <Button
                    size="sm"
                    onClick={() => setHistoryOpen(true)}
                    title="Compare versions of this workflow"
                  >
                    History
                  </Button>
                )}
                {readOnly ? (
                  <>
                    <Button onClick={clone} disabled={cloning}>
                      {cloning ? "Cloning…" : "Clone to draft"}
                    </Button>
                    <Button
                      variant="primary"
                      onClick={() => setDeployOpen(true)}
                      title="Create a deployment version from this published graph"
                    >
                      Deploy
                    </Button>
                  </>
                ) : (
                  <>
                    <Button onClick={saveNow} disabled={saveState === "saving"}>
                      Save
                    </Button>
                    <Button
                      variant="primary"
                      onClick={publish}
                      disabled={publishing || nodes.length === 0}
                      title="Validate and publish this draft, making it immutable and deployable"
                    >
                      {publishing ? "Publishing…" : "Publish"}
                    </Button>
                  </>
                )}
              </div>
              {copied !== null && (
                <span className="text-xs text-muted">
                  Copied {copied} node{copied === 1 ? "" : "s"}
                </span>
              )}
              {justPublished && readOnly && (
                <p className="max-w-sm rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs text-emerald-800 dark:border-emerald-900/60 dark:bg-emerald-950/40 dark:text-emerald-300">
                  Published v{version} — use <strong>Deploy</strong> to create a deployment
                  version from it.
                </p>
              )}
              {error && (
                <div className="max-w-sm">
                  <ErrorBox message={error} />
                </div>
              )}
              {publishIssues && (
                <PublishIssuesPanel
                  issues={publishIssues}
                  onFocusNode={focusNode}
                  onDismiss={() => setPublishIssues(null)}
                />
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

          <RunPanel
            workflowId={id}
            nodeIds={nodeIds}
            others={others}
            onStates={setRunStates}
          />
        </ReactFlow>
        </PublishIssuesContext.Provider>
        </RunStateContext.Provider>
      )}

      {editing && (
        <NodeEditorDialog
          node={editing}
          readOnly={readOnly}
          connectors={connectors}
          onConnectorsChanged={reloadConnectors}
          manifestRefs={manifestRefs}
          contractNames={contractNames}
          onContractsChanged={refreshContracts}
          toolTargets={editingToolTargets}
          onClose={() => setEditingId(null)}
          onPatch={(patch) => patchNode(editing.id, patch)}
          onDelete={() => deleteNode(editing.id)}
        />
      )}

      {deployOpen && (
        <DeployDialog workflowId={id} version={version} onClose={() => setDeployOpen(false)} />
      )}

      {historyOpen && (
        <HistoryDialog
          workflowId={id}
          currentVersion={version}
          onClose={() => setHistoryOpen(false)}
        />
      )}
    </div>
  );
}

function NodeEditorDialog({
  node,
  readOnly,
  connectors,
  onConnectorsChanged,
  manifestRefs,
  contractNames,
  onContractsChanged,
  toolTargets,
  onClose,
  onPatch,
  onDelete,
}: {
  node: Node;
  readOnly: boolean;
  connectors: ConnectorSummary[];
  onConnectorsChanged: () => void | Promise<void>;
  manifestRefs: string[];
  contractNames: string[];
  onContractsChanged?: () => void | Promise<void>;
  /** Units attached to this node via tool edges (agent nodes only). */
  toolTargets?: { id: string; label: string }[];
  onClose: () => void;
  onPatch: (patch: NodePatch) => void;
  onDelete: () => void;
}) {
  const d = node.data as {
    studioType: string;
    label: string;
    config: Cfg;
    inputContractRef?: string | null;
    outputContractRef?: string | null;
  };
  const closeRef = useRef<HTMLButtonElement>(null);
  const [tab, setTab] = useState<"config" | "activity">("config");
  // Activity mounts lazily on first visit (its fetch runs on mount) and then
  // stays mounted-but-hidden so toggling tabs doesn't refetch.
  const [activityOpened, setActivityOpened] = useState(false);

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
        // Code nodes host a real editor — give them room to breathe.
        className={`w-full rounded-xl border border-border bg-surface shadow-xl ${
          d.studioType === "code" ? "max-w-2xl" : "max-w-md"
        }`}
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

        <div className="flex gap-1.5 border-b border-border px-4 py-2.5" role="group">
          <TabButton active={tab === "config"} onClick={() => setTab("config")}>
            Config
          </TabButton>
          <TabButton
            active={tab === "activity"}
            onClick={() => {
              setTab("activity");
              setActivityOpened(true);
            }}
          >
            Activity
          </TabButton>
        </div>

        <div className="max-h-[60vh] overflow-auto p-4">
          <div className={tab === "config" ? "" : "hidden"}>
            <NodeInspector
              studioType={d.studioType}
              label={d.label}
              config={d.config}
              inputContractRef={d.inputContractRef ?? null}
              outputContractRef={d.outputContractRef ?? null}
              contractOptions={contractNames}
              toolTargets={toolTargets}
              onContractsChanged={onContractsChanged}
              readOnly={readOnly}
              dynamicOptions={{
                connectors: connectors.map((c) => c.ref),
                manifests: manifestRefs,
              }}
              connectors={connectors}
              onConnectorsChanged={onConnectorsChanged}
              onLabelChange={(label) => onPatch({ label })}
              onConfigChange={(config) => onPatch({ config })}
              onContractRefChange={(which, ref) =>
                d.studioType === "entrypoint"
                  ? // One contract, both directions: what enters the workflow is
                    // exactly what the entrypoint emits downstream.
                    onPatch({ inputContractRef: ref, outputContractRef: ref })
                  : onPatch(
                      which === "input" ? { inputContractRef: ref } : { outputContractRef: ref },
                    )
              }
            />
          </div>
          {activityOpened && (
            <div className={tab === "activity" ? "" : "hidden"}>
              <NodeActivity nodeId={node.id} />
            </div>
          )}
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

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-lg border px-3 py-1 text-xs font-medium transition-colors ${
        active
          ? "border-accent/30 bg-accent/10 text-accent"
          : "border-border text-muted hover:bg-accent/[0.04] hover:text-foreground"
      }`}
    >
      {children}
    </button>
  );
}

// Same active set as the Runs page — statuses that keep the 1.5s poll going.
const RUN_ACTIVE = new Set([
  "running",
  "pending",
  "queued",
  "in_progress",
  "paused",
  "awaiting_approval",
]);

function isActiveStatus(s: string | null | undefined): boolean {
  return !!s && RUN_ACTIVE.has(s.toLowerCase());
}

// n8n-style bottom bar: run the deployed graph from the canvas and watch the
// nodes light up live, or replay a past run's timeline as the same overlay.
// All state here is ephemeral — it never touches nodes/edges (see runState.tsx),
// so running can't dirty autosave or the undo stack.
function RunPanel({
  workflowId,
  nodeIds,
  others,
  onStates,
}: {
  workflowId: string;
  nodeIds: string[];
  others: WorkflowSummary[];
  onStates: (s: Record<string, NodeRunState>) => void;
}) {
  const [open, setOpen] = useState(false);
  // undefined = health check in flight; null = unreachable/no deployment.
  const [deployedId, setDeployedId] = useState<string | null | undefined>(undefined);
  const [payload, setPayload] = useState('{\n  "question": "What is Zeroth?"\n}');
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunStatus | null>(null);
  const [failedNode, setFailedNode] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pastOpen, setPastOpen] = useState(false);
  const [past, setPast] = useState<RunStatus[] | null>(null);
  const [pastError, setPastError] = useState<string | null>(null);
  const [selectedPast, setSelectedPast] = useState<string | null>(null);
  // The poll tick reads node ids through a ref so nodes added/moved mid-run
  // are picked up without restarting the interval.
  const nodeIdsRef = useRef(nodeIds);
  nodeIdsRef.current = nodeIds;

  // POST /v1/runs always executes the deployment's graph, so only the
  // workflow whose id matches health.graph_version_ref ("graphId@version")
  // can be run from this canvas.
  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (!cancelled) setDeployedId(h.graph_version_ref.split("@")[0]);
      })
      .catch(() => {
        if (!cancelled) setDeployedId(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const isDeployed = deployedId != null && deployedId === workflowId;
  const polling = runId !== null && (run === null || isActiveStatus(run.status));

  // Poll status + timeline while the submitted run is active; cleanup covers
  // unmount and workflow switch (the editor remounts per workflow id).
  useEffect(() => {
    if (!runId) return;
    let stopped = false;
    let timer = 0;
    async function tick(): Promise<boolean> {
      try {
        const [status, tl] = await Promise.all([
          getRun(runId!),
          getRunTimeline(runId!).catch(() => null),
        ]);
        if (stopped) return false;
        const entries = tl?.entries ?? [];
        const active = isActiveStatus(status.status);
        setRun(status);
        const failed = entries.filter((e) => entryPhase(e) === "failed");
        setFailedNode(failed.length > 0 ? failed[failed.length - 1].node_id : null);
        onStates(
          deriveNodeStates(entries, { allNodeIds: nodeIdsRef.current, runActive: active }),
        );
        return active;
      } catch (e) {
        if (!stopped) setError(errMsg(e));
        return false;
      }
    }
    (async () => {
      const active = await tick();
      if (stopped || !active) return;
      timer = window.setInterval(async () => {
        const stillActive = await tick();
        if (!stillActive) window.clearInterval(timer);
      }, 1500);
    })();
    return () => {
      stopped = true;
      if (timer) window.clearInterval(timer);
    };
  }, [runId, onStates]);

  async function submit() {
    setError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(payload);
    } catch {
      setError("Input payload is not valid JSON.");
      return;
    }
    setSubmitting(true);
    setSelectedPast(null);
    setRunId(null);
    setRun(null);
    setFailedNode(null);
    // Immediate feedback: everything waits until timeline entries arrive.
    onStates(
      Object.fromEntries(
        nodeIdsRef.current.map((n): [string, NodeRunState] => [n, { phase: "waiting" }]),
      ),
    );
    try {
      const res = await submitRun({ input_payload: parsed });
      setRunId(res.run_id);
    } catch (e) {
      setError(errMsg(e));
      onStates({});
    } finally {
      setSubmitting(false);
    }
  }

  function clear() {
    setRunId(null); // stops the poll via effect cleanup
    setRun(null);
    setFailedNode(null);
    setSelectedPast(null);
    setError(null);
    onStates({});
  }

  async function togglePast() {
    const next = !pastOpen;
    setPastOpen(next);
    if (next && past === null && pastError === null) {
      try {
        setPast((await listRuns()).runs.slice(0, 10));
      } catch (e) {
        setPastError(errMsg(e));
      }
    }
  }

  // Replay: one timeline fetch, no polling — paints the same overlay a live
  // run does, plus that run's output/failure below.
  async function selectPast(r: RunStatus) {
    setRunId(null);
    setSelectedPast(r.run_id);
    setError(null);
    try {
      const [status, tl] = await Promise.all([
        getRun(r.run_id),
        getRunTimeline(r.run_id).catch(() => null),
      ]);
      const entries = tl?.entries ?? [];
      setRun(status);
      const failed = entries.filter((e) => entryPhase(e) === "failed");
      setFailedNode(failed.length > 0 ? failed[failed.length - 1].node_id : null);
      onStates(deriveNodeStates(entries));
    } catch (e) {
      setError(errMsg(e));
    }
  }

  if (!open) {
    return (
      <Panel position="bottom-center">
        <button
          onClick={() => setOpen(true)}
          className="flex items-center gap-2 rounded-full border border-border bg-surface px-4 py-1.5 text-sm font-medium shadow-md shadow-black/[0.06] transition-colors hover:border-accent/40 hover:text-accent"
        >
          <span aria-hidden>▶</span> Run
          {run && <StatusBadge status={run.status} />}
        </button>
      </Panel>
    );
  }

  const deployedWf = others.find((w) => w.id === deployedId);

  return (
    <Panel position="bottom-center">
      <div className="w-[26rem] max-w-[90vw] rounded-xl border border-border bg-surface shadow-md shadow-black/[0.06]">
        <header className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-semibold">
            Run
            {run && <StatusBadge status={run.status} />}
            {polling && <span className="text-xs font-normal text-muted">auto-refreshing…</span>}
          </div>
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)} aria-label="Collapse run panel">
            ▾
          </Button>
        </header>

        <div className="max-h-[45vh] space-y-3 overflow-y-auto p-3">
          {deployedId === undefined ? (
            <p className="text-xs text-muted">Checking which graph is deployed…</p>
          ) : !isDeployed ? (
            <p className="text-xs text-muted">
              Only the deployed graph can run. Publish &amp; deploy this workflow
              {deployedWf ? (
                <>
                  , or open{" "}
                  <Link
                    href={`/studio/edit?id=${encodeURIComponent(deployedWf.id)}`}
                    className="font-medium text-accent hover:underline"
                  >
                    {deployedWf.name}
                  </Link>{" "}
                  to run it.
                </>
              ) : (
                " to run it here."
              )}
            </p>
          ) : (
            <>
              <Field label="Input payload (JSON)" hint="shape set by the graph's input contract">
                <Textarea
                  value={payload}
                  onChange={(e) => setPayload(e.target.value)}
                  rows={3}
                  className="font-mono text-xs"
                />
              </Field>
              <div className="flex items-center gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  onClick={submit}
                  disabled={submitting || polling}
                >
                  {submitting ? "Submitting…" : polling ? "Running…" : "Run"}
                </Button>
                {(run !== null || runId !== null) && (
                  <Button size="sm" onClick={clear}>
                    Clear
                  </Button>
                )}
              </div>
              {/* Same invocation as a shell command — the deployed graph IS an
                  API service; this is how apps outside the console call it. */}
              <details>
                <summary className="cursor-pointer text-xs font-medium text-muted transition-colors hover:text-foreground">
                  Call this API with cURL
                </summary>
                <div className="mt-2">
                  <CurlBlock
                    command={buildRunCurl(payload)}
                    secret={getApiKey() || undefined}
                  />
                </div>
              </details>
            </>
          )}

          {error && <ErrorBox message={error} />}

          {run && (
            <div className="space-y-2 text-sm">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
                <span className="font-mono">{run.run_id}</span>
                {run.current_step && <span>step: {run.current_step}</span>}
              </div>
              {run.failure_state != null && (
                <div>
                  <div className="mb-1 text-xs font-medium text-red-700 dark:text-red-400">
                    Failed
                    {failedNode && (
                      <>
                        {" "}
                        at <span className="font-mono">{failedNode}</span>
                      </>
                    )}
                  </div>
                  <Json value={run.failure_state} />
                </div>
              )}
              {run.terminal_output != null && (
                <details>
                  <summary className="cursor-pointer text-xs text-muted">Output</summary>
                  <div className="mt-1.5">
                    <Json value={run.terminal_output} />
                  </div>
                </details>
              )}
            </div>
          )}

          <div>
            <button
              onClick={togglePast}
              aria-expanded={pastOpen}
              className="flex w-full items-center justify-between text-xs font-semibold text-muted transition-colors hover:text-foreground"
            >
              Past runs
              <span
                aria-hidden
                className={`transition-transform ${pastOpen ? "rotate-180" : ""}`}
              >
                ▾
              </span>
            </button>
            {pastOpen && (
              <div className="mt-1.5">
                {pastError ? (
                  <ApiErrorNote error={pastError} />
                ) : past === null ? (
                  <Skeleton rows={2} />
                ) : past.length === 0 ? (
                  <Empty>No runs yet.</Empty>
                ) : (
                  <ul className="space-y-0.5">
                    {past.map((r) => (
                      <li key={r.run_id}>
                        <button
                          onClick={() => selectPast(r)}
                          className={`flex w-full items-center justify-between gap-2 rounded-lg border px-2 py-1.5 text-left text-xs transition-colors ${
                            selectedPast === r.run_id
                              ? "border-accent/30 bg-accent/10"
                              : "border-transparent hover:border-border hover:bg-accent/[0.04]"
                          }`}
                        >
                          <span className="truncate font-mono text-muted">{r.run_id}</span>
                          <span className="flex shrink-0 items-center gap-2">
                            {r.current_step && (
                              <span className="text-muted">{r.current_step}</span>
                            )}
                            <StatusBadge status={r.status} />
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </Panel>
  );
}

// Per-node execution history: the deployment's audit records filtered to this
// node id. Draft nodes have never run, so they land on the empty state.
function NodeActivity({ nodeId }: { nodeId: string }) {
  const [records, setRecords] = useState<NodeAuditRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRecords(null);
    setError(null);
    listNodeAudits(nodeId)
      .then((r) => {
        if (!cancelled) setRecords(r);
      })
      .catch((e) => {
        if (!cancelled) setError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (error) return <ApiErrorNote error={error} />;
  if (records === null) return <Skeleton rows={3} />;
  if (records.length === 0) {
    return (
      <Empty>
        No executions recorded for this node yet — audits appear after the deployed graph
        runs.
      </Empty>
    );
  }

  const totalCost = records.reduce((sum, r) => sum + (r.cost_usd ?? 0), 0);
  const hasCost = records.some((r) => r.cost_usd != null);

  return (
    <div className="text-sm">
      <p className="mb-2 text-xs text-muted">
        {records.length} execution{records.length === 1 ? "" : "s"} shown, newest first
        {hasCost && <> · total {fmtUsd(totalCost)}</>}
      </p>
      <ul className="divide-y divide-border">
        {records.map((r) => (
          <li key={r.audit_id} className="space-y-1.5 py-2.5 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <StatusBadge status={r.status} />
              {r.started_at && <span>{fmtTime(r.started_at)}</span>}
              {r.cost_usd != null && <span>{fmtUsd(r.cost_usd)}</span>}
              {r.attempt != null && <span>attempt {r.attempt}</span>}
            </div>
            {r.error && (
              <div className="text-xs text-red-700 dark:text-red-400">{r.error}</div>
            )}
            {(r.tool_calls?.length ?? 0) > 0 && (
              <details>
                <summary className="cursor-pointer text-xs text-muted">
                  Tool calls ({r.tool_calls!.length})
                </summary>
                <div className="mt-1.5">
                  <Json value={r.tool_calls} />
                </div>
              </details>
            )}
            {r.output_snapshot != null && (
              <details>
                <summary className="cursor-pointer text-xs text-muted">Output snapshot</summary>
                <div className="mt-1.5">
                  <Json value={r.output_snapshot} />
                </div>
              </details>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// Issue list from a failed publish (422): each entry names the check that
// failed and, where it has one, the offending node — clicking jumps there.
function PublishIssuesPanel({
  issues,
  onFocusNode,
  onDismiss,
}: {
  issues: PublishIssue[];
  onFocusNode: (nodeId: string) => void;
  onDismiss: () => void;
}) {
  const errors = issues.filter((i) => i.severity === "error").length;
  return (
    <div className="w-96 max-w-[90vw] rounded-xl border border-red-300 bg-surface shadow-md shadow-black/[0.06] dark:border-red-900/60">
      <header className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <span className="text-sm font-semibold text-red-700 dark:text-red-400">
          Can&apos;t publish yet ({errors} error{errors === 1 ? "" : "s"})
        </span>
        <Button variant="ghost" size="sm" onClick={onDismiss} aria-label="Dismiss issues">
          ✕
        </Button>
      </header>
      <ul className="max-h-[40vh] divide-y divide-border overflow-y-auto px-3 py-1">
        {issues.map((issue, i) => (
          <li key={i} className="space-y-1 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={`rounded-full px-2 py-0.5 font-medium ${
                  issue.severity === "error"
                    ? "bg-red-500/12 text-red-700 dark:text-red-400"
                    : "bg-amber-500/15 text-amber-700 dark:text-amber-400"
                }`}
              >
                {issue.severity}
              </span>
              <span className="font-mono text-muted">{issue.code}</span>
            </div>
            <p className="leading-relaxed">{issue.message}</p>
            {issue.node_id && (
              <button
                onClick={() => onFocusNode(issue.node_id!)}
                className="font-mono font-medium text-accent hover:underline"
              >
                {issue.node_id} →
              </button>
            )}
            {issue.edge_id && !issue.node_id && (
              <span className="font-mono text-muted">edge {issue.edge_id}</span>
            )}
          </li>
        ))}
      </ul>
      <p className="border-t border-border px-3 py-2 text-xs text-muted">
        Fix the errors above, then publish again. Warnings don&apos;t block.
      </p>
    </div>
  );
}

// Create a deployment version from this published graph (DEPLOYMENT_ADMIN).
// Serving the new version still needs a service restart — the note below.
function DeployDialog({
  workflowId,
  version,
  onClose,
}: {
  workflowId: string;
  version: number;
  onClose: () => void;
}) {
  const [ref, setRef] = useState("");
  const [prefilled, setPrefilled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<DeploymentSummary | null>(null);

  // Prefill with the serving deployment's ref — deploying the next version of
  // the deployment this service runs is the common upgrade path.
  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (!cancelled) {
          setRef((r) => r || h.deployment_ref);
          setPrefilled(true);
        }
      })
      .catch(() => {
        if (!cancelled) setPrefilled(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function deploy() {
    if (!ref.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setCreated(
        await createDeployment({
          deployment_ref: ref.trim(),
          graph_id: workflowId,
          graph_version: version,
        }),
      );
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-20"
      onMouseDown={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Deploy workflow"
        className="w-full max-w-md rounded-xl border border-border bg-surface shadow-xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="text-sm font-semibold">Deploy published v{version}</div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </header>

        <div className="space-y-3 p-4 text-sm">
          {created ? (
            <>
              <p className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-xs text-emerald-800 dark:border-emerald-900/60 dark:bg-emerald-950/40 dark:text-emerald-300">
                Deployment <strong>{created.deployment_ref}</strong> v{created.version} created
                from <Mono>{created.graph_version_ref}</Mono>.
              </p>
              <div className="text-xs leading-relaxed text-muted">
                A service instance serves one deployment per process, so the new version
                isn&apos;t live yet. Restart the service with
                <pre className="mt-1.5 overflow-x-auto rounded-lg bg-zinc-50 p-2 font-mono text-xs text-zinc-700 ring-1 ring-border dark:bg-zinc-900/60 dark:text-zinc-300">
                  {`ZEROTH_DEPLOYMENT_REF=${created.deployment_ref}`}
                </pre>
                to serve it.
              </div>
            </>
          ) : (
            <>
              <p className="text-xs leading-relaxed text-muted">
                Creates a new deployment version pinned to this published graph. Requires a
                key with deployment-admin permission.
              </p>
              <Field
                label="Deployment ref"
                hint="new versions of an existing ref stack under it"
              >
                <Input
                  value={ref}
                  onChange={(e) => setRef(e.target.value)}
                  placeholder={prefilled ? "my-service" : "loading…"}
                  className="font-mono"
                />
              </Field>
              {error && <ApiErrorNote error={error} />}
            </>
          )}
        </div>

        <footer className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          {created ? (
            <Button variant="primary" size="sm" onClick={onClose}>
              Done
            </Button>
          ) : (
            <>
              <Button size="sm" onClick={onClose}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={deploy}
                disabled={busy || !ref.trim()}
              >
                {busy ? "Deploying…" : "Deploy"}
              </Button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}

const DIFF_BUCKETS: { key: keyof WorkflowDiff; label: string }[] = [
  { key: "node_changes", label: "Nodes" },
  { key: "edge_changes", label: "Edges" },
  { key: "condition_changes", label: "Conditions" },
  { key: "contract_changes", label: "Contracts" },
  { key: "policy_changes", label: "Policies" },
  { key: "memory_connector_changes", label: "Memory connectors" },
  { key: "executable_unit_binding_changes", label: "Executable bindings" },
];

const CHANGE_TONE: Record<DiffEntry["change_type"], string> = {
  added: "bg-emerald-500/12 text-emerald-700 dark:text-emerald-400",
  removed: "bg-red-500/12 text-red-700 dark:text-red-400",
  modified: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
};

// Version history as a structured diff ("git diff for graphs"): pick two
// versions of this workflow id and see what changed, grouped by category.
function HistoryDialog({
  workflowId,
  currentVersion,
  onClose,
}: {
  workflowId: string;
  currentVersion: number;
  onClose: () => void;
}) {
  const [left, setLeft] = useState(currentVersion - 1);
  const [right, setRight] = useState(currentVersion);
  const [diff, setDiff] = useState<WorkflowDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const versions = Array.from({ length: currentVersion }, (_, i) => i + 1);

  useEffect(() => {
    let cancelled = false;
    setDiff(null);
    setError(null);
    diffWorkflow(workflowId, left, right)
      .then((d) => {
        if (!cancelled) setDiff(d);
      })
      .catch((e) => {
        if (!cancelled) setError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [workflowId, left, right]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isEmpty =
    diff !== null && DIFF_BUCKETS.every(({ key }) => (diff[key] as DiffEntry[]).length === 0);
  const selectCls =
    "rounded-lg border border-border bg-surface px-2 py-1 text-xs focus-visible:border-accent";

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-16"
      onMouseDown={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Version history"
        className="w-full max-w-2xl rounded-xl border border-border bg-surface shadow-xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <div className="text-sm font-semibold">Version history</div>
            <div className="text-xs text-muted">
              structured diff between two versions of this workflow
            </div>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <label className="flex items-center gap-1.5">
              <span className="text-muted">from</span>
              <select
                value={left}
                onChange={(e) => setLeft(Number(e.target.value))}
                className={selectCls}
                aria-label="Left version"
              >
                {versions.map((v) => (
                  <option key={v} value={v}>
                    v{v}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1.5">
              <span className="text-muted">to</span>
              <select
                value={right}
                onChange={(e) => setRight(Number(e.target.value))}
                className={selectCls}
                aria-label="Right version"
              >
                {versions.map((v) => (
                  <option key={v} value={v}>
                    v{v}
                  </option>
                ))}
              </select>
            </label>
            <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
              ✕
            </Button>
          </div>
        </header>

        <div className="max-h-[65vh] space-y-4 overflow-y-auto p-4">
          {error ? (
            <ApiErrorNote error={error} />
          ) : diff === null ? (
            <Skeleton rows={3} />
          ) : isEmpty ? (
            <Empty>
              No differences between v{left} and v{right}.
            </Empty>
          ) : (
            DIFF_BUCKETS.map(({ key, label }) => {
              const entries = diff[key] as DiffEntry[];
              if (entries.length === 0) return null;
              return (
                <section key={key}>
                  <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-muted">
                    {label} ({entries.length})
                  </h3>
                  <ul className="divide-y divide-border rounded-lg border border-border">
                    {entries.map((e, i) => (
                      <li key={`${e.entity_id}-${i}`} className="space-y-1.5 px-3 py-2 text-sm">
                        <div className="flex flex-wrap items-center gap-2">
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs font-medium ${CHANGE_TONE[e.change_type]}`}
                          >
                            {e.change_type}
                          </span>
                          <Mono>{e.entity_id}</Mono>
                          {e.changed_fields.length > 0 && (
                            <span className="text-xs text-muted">
                              {e.changed_fields.join(", ")}
                            </span>
                          )}
                        </div>
                        {(e.before !== null || e.after !== null) && (
                          <details>
                            <summary className="cursor-pointer text-xs text-muted">
                              before / after
                            </summary>
                            <div className="mt-1.5 grid gap-2 sm:grid-cols-2">
                              <div>
                                <div className="mb-1 text-xs text-muted">v{left}</div>
                                {e.before !== null ? <Json value={e.before} /> : <Empty>—</Empty>}
                              </div>
                              <div>
                                <div className="mb-1 text-xs text-muted">v{right}</div>
                                {e.after !== null ? <Json value={e.after} /> : <Empty>—</Empty>}
                              </div>
                            </div>
                          </details>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
