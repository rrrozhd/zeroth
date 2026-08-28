"use client";

import "@xyflow/react/dist/style.css";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { createPortal } from "react-dom";
import {
  addEdge,
  Background,
  BackgroundVariant,
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
  type Viewport,
} from "@xyflow/react";
import { DEFAULT_CONFIG, NodeInspector } from "@/app/components/NodeInspector";
import { NODE_META, NodeGlyph, nodeMetaColor } from "@/app/components/nodeMeta";
import {
  deriveNodeStates,
  entryPhase,
  PublishIssuesContext,
  RunStateContext,
  type NodeRunState,
} from "@/app/components/runState";
import { StudioNodeView, type Port } from "@/app/components/StudioNodeView";
import { StudioEdgeView } from "@/app/components/StudioEdgeView";
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
  createContract,
  createDeployment,
  diffWorkflow,
  errMsg,
  getHealth,
  getInputContract,
  getRun,
  getRunTimeline,
  getWorkflow,
  listConnectors,
  listContracts,
  listManifests,
  listNodeAudits,
  listNodeTypes,
  listRuns,
  listTemplates,
  listWorkflows,
  publishIssuesOf,
  preflightWorkflow,
  publishWorkflow,
  submitRun,
  updateWorkflow,
  verifyWorkflowProviders,
  type ConnectorSummary,
  type DeploymentSummary,
  type DiffEntry,
  type NodeAuditRecord,
  type NodeType,
  type PublishIssue,
  type RunStatus,
  type StudioEdge,
  type StudioEdgeInput,
  type StudioNode,
  type Template,
  type WorkflowDetail,
  type WorkflowDiff,
  type WorkflowPreflight,
  type LiveProviderVerification,
  type WorkflowSummary,
} from "@/app/lib/api";
import { FALLBACK_NODE_TYPES, IMPORTED_CATEGORY, normalizeNodeType } from "@/app/lib/nodeTypes";
import { setLastWorkflowId } from "@/app/lib/lastWorkflow";
import { WORKFLOW_TEMPLATES } from "@/app/lib/templates";
// P0 design-system primitives. Aliased so the toolbar can use the console's
// tinted-teal Button/Pill without colliding with the legacy ui.tsx `Button`
// (which is still used, with its size/ghost variants, by the modals + run panel).
import {
  Button as PButton,
  ConsoleMeta,
  ConsoleNotice,
  Pill,
} from "@/app/components/primitives";

import { canDeployWorkflow, canRunWorkflow, servedGraphId } from "./runEligibility";
import {
  connectionClosesCycle,
  describeGraphEdges,
  layoutGraphNodes,
  ifRouteCondition,
  loopRouteCondition,
  shouldAutoLayoutLoopGraph,
  type EdgePresentation,
} from "./graphPresentation";
import { examplePayloadFromSchema } from "../../lib/runPayload";
import { StudioDialog } from "./StudioDialog";

const nodeTypes = { studio: StudioNodeView };
const edgeTypes = { studio: StudioEdgeView };
const HISTORY_GUARD_KEY = "__zerothStudioNavigationGuard";

type Cfg = Record<string, unknown>;
type SaveState = "idle" | "dirty" | "saving" | "saved";
type StudioExecutionSettings = NonNullable<WorkflowDetail["execution_settings"]>;
const DEFAULT_STUDIO_EXECUTION_SETTINGS: StudioExecutionSettings = {
  max_total_steps: 1000,
  max_total_runtime_seconds: null,
  max_visits_per_node: 10,
  max_visits_per_edge: null,
  default_timeout_seconds: null,
};

function shouldWarnBeforeLeaving(state: SaveState): boolean {
  return state === "dirty" || state === "saving";
}

function confirmNavigation(
  shouldWarn: boolean,
  confirm: (message: string) => boolean = window.confirm,
): boolean {
  return !shouldWarn || confirm("Discard unsaved workflow changes?");
}

function pushHistoryGuard(href: string) {
  const state = window.history.state;
  window.history.pushState(
    {
      ...(state && typeof state === "object" ? state : {}),
      [HISTORY_GUARD_KEY]: true,
    },
    "",
    href,
  );
}

function confirmGraphDeletion(
  nodes: Node[],
  edges: Edge[],
  confirm: (message: string) => boolean = window.confirm,
): boolean {
  const nodeScope = nodes
    .map((node) => `${String(node.data.label ?? node.id)} (${node.id})`)
    .join(", ");
  const edgeScope = edges.map((edge) => edge.id).join(", ");
  return confirm(
    `Delete workflow elements?\nNodes: ${nodeScope || "none"}\nEdges: ${edgeScope || "none"}\nThis cannot be undone.`,
  );
}

async function publishSavedDraft(
  save: () => Promise<boolean>,
  publish: () => Promise<unknown>,
): Promise<boolean> {
  if (!(await save())) return false;
  await publish();
  return true;
}

// Editable slice of a canvas node's data (inspector writes through patchNode).
type NodePatch = Partial<{
  label: string;
  config: Cfg;
  inputContractRef: string | null;
  outputContractRef: string | null;
  parallelConfig: ParallelConfig | null;
  joinConfig: JoinConfig | null;
}>;

type ParallelConfig = {
  split_path: string;
  merge_strategy?: "collect" | "reduce" | "merge" | "custom";
  reducer_ref?: string | null;
  fail_mode?: "fail_fast" | "best_effort";
  max_branches?: number | null;
  max_concurrency?: number | null;
  batch_size?: number | null;
  branch_timeout_seconds?: number | null;
};

type JoinConfig = {
  merge_strategy?: "collect" | "reduce" | "merge" | "custom";
  reducer_ref?: string | null;
  merge_path?: string | null;
};

type AdvancedEdgeData = {
  kind: EdgeKind;
  condition?: StudioEdge["condition"];
  mapping?: StudioEdge["mapping"];
  enabled: boolean;
  presentation?: EdgePresentation;
};

type EdgeMappingOperation = NonNullable<
  NonNullable<StudioEdge["mapping"]>["operations"]
>[number];

function portsFor(type: string, types: NodeType[]): Port[] {
  return (types.find((t) => t.type === type)?.ports ?? []) as Port[];
}

// Tool edges (agent Tools handle -> unit Tool handle) are a separate set of
// edges from the data flow: violet and dashed on the canvas, kind="tool" in
// the graph. The handle ids are the ground truth for which set an edge is in.
type EdgeKind = "data" | "tool";

// Handoff edge styling: data flow is a solid slate hairline; agent->tool edges
// are a dashed violet hairline at reduced opacity so the two edge sets read as
// distinct planes on the canvas.
const DATA_EDGE_STYLE = { stroke: "#3f4757", strokeWidth: 1.5 };
const TOOL_EDGE_STYLE = {
  stroke: "#c4b5fd",
  strokeDasharray: "4 4",
  strokeWidth: 1.5,
  opacity: 0.75,
};

function edgeKindOf(e: {
  sourceHandle?: string | null;
  data?: Record<string, unknown>;
}): EdgeKind {
  if ((e.data as { kind?: string } | undefined)?.kind === "tool") return "tool";
  return e.sourceHandle === "tools" ? "tool" : "data";
}

/** Canvas props (styling + kind marker) for an edge of the given kind. */
function edgeKindProps(kind: EdgeKind): Partial<Edge> {
  return kind === "tool"
    ? { type: "studio", data: { kind, enabled: true }, style: TOOL_EDGE_STYLE }
    : { type: "studio", data: { kind, enabled: true }, style: DATA_EDGE_STYLE };
}

function withNodeEvidence<T extends Node>(node: T): T {
  return {
    ...node,
    ariaLabel: `Workflow node ${node.id}`,
    domAttributes: {
      ...(node.domAttributes ?? {}),
      "data-evidence-id": `studio.node.${node.id}`,
    },
  };
}

function withEdgeEvidence<T extends Edge>(edge: T): T {
  return {
    ...edge,
    ariaLabel: `Workflow edge ${edge.id}`,
    domAttributes: {
      ...(edge.domAttributes ?? {}),
      "data-evidence-id": `studio.edge.${edge.id}`,
    },
  };
}

// Structural signature for autosave + undo history. Selection lives on the
// node objects (`selected`), so key off ids/positions/data only — clicking a
// node must not schedule a save or record a history entry.
function graphSig(nodes: Node[], edges: Edge[]): string {
  return JSON.stringify([
    nodes.map((n) => [n.id, n.position.x, n.position.y, n.data]),
    edges.map((e) => [
      e.id,
      e.source,
      e.sourceHandle ?? null,
      e.target,
      e.targetHandle ?? null,
      e.data,
    ]),
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
    parallelConfig?: ParallelConfig | null;
    joinConfig?: JoinConfig | null;
    position: { x: number; y: number };
  }[];
  edges: {
    id: string;
    source: string;
    target: string;
    sourceHandle: string | null;
    targetHandle: string | null;
    data?: AdvancedEdgeData;
  }[];
  ts: number;
};

function toRfNodes(detail: WorkflowDetail, types: NodeType[]): Node[] {
  return detail.nodes.map((n) => {
    const studioType = normalizeNodeType(n.type);
    const data = (n.data ?? {}) as {
      label?: string;
      config?: Cfg;
      input_contract_ref?: string | null;
      output_contract_ref?: string | null;
      parallel_config?: ParallelConfig | null;
      join_config?: JoinConfig | null;
    };
    return withNodeEvidence({
      id: n.id,
      type: "studio",
      position: { x: n.position.x, y: n.position.y },
      data: {
        label: data.label || n.id,
        studioType,
        ports: portsFor(studioType, types),
        config: data.config ?? {},
        // Node-level contract bindings must round-trip — dropping them here
        // would silently strip contracts from graphs authored in Python.
        inputContractRef: data.input_contract_ref ?? null,
        outputContractRef: data.output_contract_ref ?? null,
        parallelConfig: data.parallel_config ?? null,
        joinConfig: data.join_config ?? null,
      },
    });
  });
}

// Graphs authored in Python (or any API client) carry no canvas handle ids.
// Our handles are all explicitly named, and React Flow drops an edge whose
// handle id does not match one on the node — so without defaults such graphs
// rendered with NO edges at all. Every node type uses the same port ids
// (input-data/output-data, tools/tool-input), so defaulting by edge kind is
// exact, and the first save persists the ids.
function defaultHandles(kind: EdgeKind): { sourceHandle: string; targetHandle: string } {
  return kind === "tool"
    ? { sourceHandle: "tools", targetHandle: "tool-input" }
    : { sourceHandle: "output-data", targetHandle: "input-data" };
}

function toRfEdges(detail: WorkflowDetail): Edge[] {
  const presentations = describeGraphEdges(
    detail.edges,
    new Map(detail.nodes.map((node) => {
      const data = (node.data ?? {}) as { label?: string };
      return [node.id, data.label || node.id];
    })),
  );
  return detail.edges.map((e) => {
    const kind = e.kind ?? edgeKindOf({ sourceHandle: e.source_handle });
    const fallback = defaultHandles(kind);
    return withEdgeEvidence({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.source_handle ?? fallback.sourceHandle,
      targetHandle: e.target_handle ?? fallback.targetHandle,
      ...edgeKindProps(kind),
      data: {
        kind,
        condition: e.condition ?? null,
        mapping: e.mapping ?? null,
        enabled: e.enabled,
        presentation: presentations.get(e.id),
      } satisfies AdvancedEdgeData,
    });
  });
}

function withEdgePresentations(edges: Edge[]): Edge[] {
  const presentations = describeGraphEdges(
    edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      condition: (edge.data as AdvancedEdgeData | undefined)?.condition ?? null,
    })),
  );
  return edges.map((edge) => withEdgeEvidence({
      ...edge,
      type: "studio",
      data: {
        kind: edgeKindOf(edge),
        enabled: true,
        ...(edge.data as AdvancedEdgeData | undefined),
        presentation: presentations.get(edge.id),
      } satisfies AdvancedEdgeData,
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
      parallelConfig?: ParallelConfig | null;
      joinConfig?: JoinConfig | null;
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
        parallel_config: d.parallelConfig ?? null,
        join_config: d.joinConfig ?? null,
      },
    };
  });
}

function toStudioEdges(edges: Edge[]): StudioEdgeInput[] {
  return edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    source_handle: e.sourceHandle ?? null,
    target_handle: e.targetHandle ?? null,
    kind: edgeKindOf(e),
    condition: ((e.data as AdvancedEdgeData | undefined)?.condition ?? null),
    mapping: ((e.data as AdvancedEdgeData | undefined)?.mapping ?? null),
    enabled: (e.data as AdvancedEdgeData | undefined)?.enabled ?? true,
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

// The camera position survives navigation: pan/zoom is stored per workflow in
// sessionStorage and restored on mount, so leaving the editor and coming back
// does not snap to the fit-view default.
function viewportStorageKey(workflowId: string): string {
  return `zeroth.studio.viewport.${workflowId}`;
}

function readStoredViewport(workflowId: string): Viewport | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(viewportStorageKey(workflowId));
    if (!raw) return null;
    const vp = JSON.parse(raw) as Viewport;
    return typeof vp?.x === "number" && typeof vp?.y === "number" && typeof vp?.zoom === "number"
      ? vp
      : null;
  } catch {
    return null;
  }
}

// The Workflows widget can be dragged anywhere on the canvas; its offset from
// the default top-right anchor is a global UI preference (localStorage), so it
// stays where the user put it across workflows and sessions.
type Offset = { x: number; y: number };
const WORKFLOWS_OFFSET_KEY = "zeroth.studio.workflowsOffset";

function readWorkflowsOffset(): Offset {
  if (typeof window === "undefined") return { x: 0, y: 0 };
  try {
    const o = JSON.parse(window.localStorage.getItem(WORKFLOWS_OFFSET_KEY) ?? "");
    return typeof o?.x === "number" && typeof o?.y === "number" ? o : { x: 0, y: 0 };
  } catch {
    return { x: 0, y: 0 };
  }
}

function Editor({ id }: { id: string }) {
  // Read once per mount (the editor remounts per workflow id).
  const initialViewportRef = useRef<Viewport | null>(readStoredViewport(id));
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [name, setName] = useState("");
  const [status, setStatus] = useState<string>("");
  // The graph's entrypoint node id ("" = unset). Required to publish; saved
  // through PUT alongside the structure ("" clears it server-side).
  const [entryStep, setEntryStep] = useState("");
  const [executionSettings, setExecutionSettings] = useState<StudioExecutionSettings>(
    DEFAULT_STUDIO_EXECUTION_SETTINGS,
  );
  const [version, setVersion] = useState(1);
  const [palette, setPalette] = useState<NodeType[]>([]);
  const [rf, setRf] = useState<ReactFlowInstance<Node, Edge> | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingEdgeId, setEditingEdgeId] = useState<string | null>(null);
  const [loopSafetyOpen, setLoopSafetyOpen] = useState(false);
  const paneRef = useRef<HTMLDivElement>(null);
  const nodeMenuRef = useRef<HTMLDivElement>(null);
  const workflowMenuRef = useRef<HTMLDivElement>(null);
  const placementGhostRef = useRef<HTMLDivElement>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
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
  const saveNowRef = useRef<() => Promise<boolean>>(async () => true);
  const continueHistoryRef = useRef(false);
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
  // Prompt templates are tenant-scoped and readable by any role with RUN_READ.
  // Keep a permission/service error separate so an existing saved reference is
  // never mistaken for an empty library and silently discarded.
  const [templates, setTemplates] = useState<Template[]>([]);
  const [templateAccessError, setTemplateAccessError] = useState<string | null>(null);
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
  // Draggable Workflows widget: offset from its default top-right anchor.
  const [wfOffset, setWfOffset] = useState<Offset>(() => readWorkflowsOffset());
  const wfOffsetRef = useRef(wfOffset);
  wfOffsetRef.current = wfOffset;
  const wfDragRef = useRef<{ px: number; py: number; bx: number; by: number } | null>(null);
  const onWfPointerDown = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    wfDragRef.current = {
      px: e.clientX,
      py: e.clientY,
      bx: wfOffsetRef.current.x,
      by: wfOffsetRef.current.y,
    };
  }, []);
  const onWfPointerMove = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    const d = wfDragRef.current;
    if (!d) return;
    setWfOffset({ x: d.bx + (e.clientX - d.px), y: d.by + (e.clientY - d.py) });
  }, []);
  const onWfPointerUp = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    const d = wfDragRef.current;
    if (!d) return;
    wfDragRef.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    // Compute the final offset from the drag origin + pointer delta so
    // persistence never depends on a state ref that a fast drag may not have
    // re-rendered yet.
    const final = { x: d.bx + (e.clientX - d.px), y: d.by + (e.clientY - d.py) };
    setWfOffset(final);
    try {
      window.localStorage.setItem(WORKFLOWS_OFFSET_KEY, JSON.stringify(final));
    } catch {
      /* storage unavailable — position persistence is best-effort */
    }
  }, []);
  // Run overlay per node id, painted by the RunPanel. Lives in context (not
  // node.data) so runs never dirty the autosave signature or undo history.
  const [runStates, setRunStates] = useState<Record<string, NodeRunState>>({});
  // Structured issue list from the last failed publish (422); same context
  // rule as run state — never stored in node.data.
  const [publishIssues, setPublishIssues] = useState<PublishIssue[] | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [preflighting, setPreflighting] = useState(false);
  const [preflight, setPreflight] = useState<WorkflowPreflight | null>(null);
  const [liveVerification, setLiveVerification] = useState<LiveProviderVerification | null>(null);
  const [verifyingProviders, setVerifyingProviders] = useState(false);
  const [deployOpen, setDeployOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [nodeMenuOpen, setNodeMenuOpen] = useState(false);
  const [workflowMenuOpen, setWorkflowMenuOpen] = useState(false);
  const [placementType, setPlacementType] = useState<NodeType | null>(null);

  const readOnly = status === "published";
  // A published graph is immutable, so it can never have user-authored state
  // worth guarding. During hydration React may briefly carry a stale draft
  // save state across the publish/load transition; letting that state install
  // the document-level click guard makes every sidebar link appear dead.
  const navigationBlocked = !readOnly && shouldWarnBeforeLeaving(saveState);
  const sig = useMemo(() => graphSig(nodes, edges), [nodes, edges]);
  const draftSig = JSON.stringify([sig, name, entryStep, executionSettings]);
  const draftSigRef = useRef(draftSig);
  draftSigRef.current = draftSig;
  const nodeIds = useMemo(() => nodes.map((n) => n.id), [nodes]);

  useEffect(() => {
    if (!nodeMenuOpen && !workflowMenuOpen && placementType === null) return;

    function closeOnOutsidePointer(event: PointerEvent) {
      if (
        nodeMenuOpen &&
        event.target instanceof globalThis.Node &&
        !nodeMenuRef.current?.contains(event.target)
      ) {
        setNodeMenuOpen(false);
      }
      if (
        workflowMenuOpen &&
        event.target instanceof globalThis.Node &&
        !workflowMenuRef.current?.contains(event.target)
      ) {
        setWorkflowMenuOpen(false);
      }
    }

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setNodeMenuOpen(false);
      setWorkflowMenuOpen(false);
      setPlacementType(null);
    }

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [nodeMenuOpen, placementType, workflowMenuOpen]);

  useEffect(() => {
    if (!nodeMenuOpen) return;
    const frame = window.requestAnimationFrame(() => {
      nodeMenuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [nodeMenuOpen]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [detail, types, conns, manifests, contracts, templateResult, all] = await Promise.all([
        getWorkflow(id),
        // The registry is static server-side; falling back to its mirror keeps
        // the palette and port handles alive when only this call fails.
        listNodeTypes().catch(() => FALLBACK_NODE_TYPES),
        listConnectors().catch(() => []),
        listManifests().catch(() => []),
        listContracts().catch(() => []),
        listTemplates()
          .then((result) => ({ templates: result.templates, error: null }))
          .catch((templateError: unknown) => ({ templates: [] as Template[], error: errMsg(templateError) })),
        listWorkflows().catch(() => []),
      ]);
      const restoredExecutionSettings =
        detail.execution_settings ?? DEFAULT_STUDIO_EXECUTION_SETTINGS;
      setName(detail.name);
      setStatus(detail.status);
      setEntryStep(detail.entry_step ?? "");
      setExecutionSettings(restoredExecutionSettings);
      setVersion(detail.version);
      setPalette(types);
      setConnectors(conns);
      setManifestRefs(
        manifests.filter((m) => m.kind === "executable_unit").map((m) => m.manifest_ref),
      );
      setContractNames(contracts.map((c) => c.name));
      setTemplates(templateResult.templates);
      setTemplateAccessError(templateResult.error);
      setOthers(all.filter((w) => w.id !== id));
      const rawNodes = toRfNodes(detail, types);
      const rfEdges = toRfEdges(detail);
      const shouldArrangeLoop = shouldAutoLayoutLoopGraph(
        rawNodes.map((node) => ({ id: node.id, x: node.position.x, y: node.position.y })),
        detail.edges,
      );
      const arrangedPositions = shouldArrangeLoop
        ? layoutGraphNodes(rawNodes.map((node) => node.id), detail.edges)
        : null;
      const rfNodes = arrangedPositions
        ? rawNodes.map((node) => ({
            ...node,
            position: arrangedPositions.get(node.id) ?? node.position,
          }))
        : rawNodes;
      setNodes(rfNodes);
      setEdges(rfEdges);
      setLastWorkflowId(id);
      // Baseline for autosave + undo: the freshly loaded graph counts as saved
      // and is history entry 0 (clone() re-loads, so this also resets both).
      lastSavedSigRef.current = JSON.stringify([
        graphSig(rfNodes, rfEdges),
        detail.name,
        detail.entry_step ?? "",
        restoredExecutionSettings,
      ]);
      lastHistSigRef.current = graphSig(rfNodes, rfEdges);
      histRef.current = { stack: [{ nodes: rfNodes, edges: rfEdges }], index: 0 };
      setHistState({ canUndo: false, canRedo: false });
      loadedRef.current = true;
    } catch (e) {
      // A stored id that 404s must not keep steering the Studio nav link back
      // to a dead editor page.
      if (e instanceof ApiError && e.status === 404) setLastWorkflowId(null);
      // The empty state still offers "Insert example graph", which needs a
      // palette for ports — keep the offline registry available.
      setPalette((p) => (p.length > 0 ? p : FALLBACK_NODE_TYPES));
      // A bare status line ("404 Not Found") reads like a broken page; name
      // the workflow and point at the likely fix (a stale API address).
      setError(
        `Couldn't load workflow "${id}" — ${errMsg(e)}. If the API address is wrong, update it under Connection settings in the navigation rail.`,
      );
    } finally {
      setLoading(false);
    }
  }, [id, setNodes, setEdges]);

  useEffect(() => {
    load();
  }, [load]);

  // Published graphs are immutable, while connector, contract, manifest, and
  // child-deployment readiness can change around them. Recompute the safe,
  // provider-free preflight when a published graph is reopened so refresh does
  // not erase the operator's readiness view or preserve stale evidence.
  useEffect(() => {
    if (status !== "published") {
      setPreflight(null);
      return;
    }

    let cancelled = false;
    setPreflighting(true);
    void preflightWorkflow(id)
      .then((result) => {
        if (!cancelled) setPreflight(result);
      })
      .catch((preflightError) => {
        if (!cancelled) {
          setPreflight(null);
          setError(errMsg(preflightError));
        }
      })
      .finally(() => {
        if (!cancelled) setPreflighting(false);
      });

    return () => {
      cancelled = true;
    };
  }, [id, status]);

  useEffect(() => {
    if (
      rf === null ||
      loading ||
      nodes.length === 0 ||
      typeof window.matchMedia !== "function" ||
      !window.matchMedia("(max-width: 900px)").matches
    ) {
      return;
    }
    const timeout = window.setTimeout(() => {
      rf.fitView({ maxZoom: 1, padding: 0.25 });
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [loading, nodes.length, rf]);

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
      const sourceType = (nodes.find((node) => node.id === c.source)?.data as {
        studioType?: string;
      } | undefined)?.studioType;
      const targetType = (nodes.find((node) => node.id === c.target)?.data as {
        studioType?: string;
      } | undefined)?.studioType;
      const routeCondition = sourceType === "loop"
        ? loopRouteCondition(c.source, c.sourceHandle)
        : sourceType === "if"
          ? ifRouteCondition(c.source, c.sourceHandle)
          : null;
      const returnCondition = targetType === "loop" && connectionClosesCycle(
        edges.map((edge) => ({ source: edge.source, target: edge.target })),
        c.source,
        c.target,
      )
        ? {
            expression: "True",
            branch_rule: "expression" as const,
            allow_cycle_traversal: true,
            metadata: { purpose: "loop_return" },
          }
        : null;
      const kind = c.sourceHandle === "tools" ? "tool" : "data";
      const props = edgeKindProps(kind);
      setEdges((es) =>
        withEdgePresentations(addEdge(
          {
            ...c,
            id: `e-${c.source}.${c.sourceHandle}-${c.target}.${c.targetHandle}`,
            ...props,
            data: {
              kind,
              enabled: true,
              condition: routeCondition ?? returnCondition,
            } satisfies AdvancedEdgeData,
          },
          es,
        )),
      );
    },
    [edges, nodes, setEdges],
  );

  const addNode = useCallback(
    (t: NodeType, droppedAt?: { x: number; y: number }) => {
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
        const position = droppedAt
          ? { x: droppedAt.x - 104, y: droppedAt.y - 36 }
          : center
          ? { x: center.x - 104 + jitter, y: center.y - 36 + jitter }
          : { x: 80 + ns.length * 30, y: 80 + ns.length * 30 };
        return ns.map((node) => ({ ...node, selected: false })).concat(withNodeEvidence({
          id: newId,
          type: "studio",
          position,
          selected: true,
          data: {
            label: t.label,
            studioType: t.type,
            ports: t.ports,
            config: { ...(DEFAULT_CONFIG[t.type] ?? {}) },
            inputContractRef: null,
            outputContractRef: null,
            parallelConfig: null,
            joinConfig: null,
          },
        }));
      });
      if (t.type === "entrypoint") setEntryStep(newId);
    },
    [rf, setNodes],
  );

  const beginPlacement = useCallback((type: NodeType) => {
    setNodeMenuOpen(false);
    setPlacementType(type);
  }, []);

  const toggleNodeMenu = useCallback(() => {
    if (readOnly) return;
    setPlacementType(null);
    setNodeMenuOpen((open) => !open);
    setNodes((current) => current.map((node) => ({ ...node, selected: false })));
  }, [readOnly, setNodes]);

  const placeNode = useCallback(
    (event: ReactMouseEvent) => {
      if (readOnly || placementType === null) return;
      const position = rf
        ? rf.screenToFlowPosition({ x: event.clientX, y: event.clientY })
        : { x: event.clientX, y: event.clientY };
      addNode(placementType, position);
      setPlacementType(null);
    },
    [addNode, placementType, readOnly, rf],
  );

  const trackPlacement = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (placementType === null || placementGhostRef.current === null) return;
      const left = Math.min(event.clientX + 14, window.innerWidth - 222);
      const top = Math.min(event.clientY + 14, window.innerHeight - 86);
      placementGhostRef.current.style.transform = `translate3d(${Math.max(8, left)}px, ${Math.max(8, top)}px, 0)`;
    },
    [placementType],
  );

  const selectNode = useCallback(
    (_event: ReactMouseEvent, selected: Node) => {
      setNodeMenuOpen(false);
      setPlacementType(null);
      setNodes((current) =>
        current.map((node) => ({ ...node, selected: node.id === selected.id })),
      );
      setEditingId(selected.id);
    },
    [setNodes],
  );

  // Populate the empty canvas with a small working example (the RAG template)
  // so a fresh draft never has to start from nothing.
  const insertExample = useCallback(async () => {
    const t = WORKFLOW_TEMPLATES[0];
    // Contract sync is best-effort: with the API unreachable the example must
    // still insert locally, or the button reads as dead.
    try {
      const existingContracts = new Set((await listContracts()).map((item) => item.name));
      await Promise.all(
        t.contracts
          .filter((item) => !existingContracts.has(item.name))
          .map((item) => createContract(item)),
      );
    } catch {
      /* contracts register on the next save against a live API */
    }
    setNodes(
      t.nodes.map((n) => {
        const d = (n.data ?? {}) as {
          label?: string;
          config?: Cfg;
          input_contract_ref?: string | null;
          output_contract_ref?: string | null;
          parallel_config?: ParallelConfig | null;
          join_config?: JoinConfig | null;
        };
        return withNodeEvidence({
          id: n.id,
          type: "studio",
          position: { x: n.position.x, y: n.position.y },
          data: {
            label: d.label ?? n.id,
            studioType: n.type,
            ports: portsFor(n.type, palette),
            config: d.config ?? {},
            inputContractRef: d.input_contract_ref ?? null,
            outputContractRef: d.output_contract_ref ?? null,
            parallelConfig: d.parallel_config ?? null,
            joinConfig: d.join_config ?? null,
          },
        });
      }),
    );
    setEdges(
      withEdgePresentations(t.edges.map((e) => {
        const kind = e.kind ?? edgeKindOf({ sourceHandle: e.source_handle });
        return {
          id: e.id,
          source: e.source,
          target: e.target,
          sourceHandle: e.source_handle ?? undefined,
          targetHandle: e.target_handle ?? undefined,
          ...edgeKindProps(kind),
          data: {
            kind,
            condition: e.condition ?? null,
            mapping: e.mapping ?? null,
            enabled: e.enabled ?? true,
          } satisfies AdvancedEdgeData,
        };
      })),
    );
    setEntryStep("start");
    await refreshContracts();
    window.setTimeout(() => rf?.fitView({ maxZoom: 1, padding: 0.25 }), 0);
  }, [palette, refreshContracts, rf, setNodes, setEdges]);

  // One-click cleanup: topological left-to-right layout (roots on the left,
  // each node one column right of its furthest predecessor), columns centered
  // vertically, then re-center the viewport on the result.
  const tidyLayout = useCallback(() => {
    setNodes((ns) => {
      if (ns.length < 2) return ns;
      const pos = layoutGraphNodes(
        ns.map((node) => node.id),
        edges.map((edge) => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          condition: (edge.data as AdvancedEdgeData | undefined)?.condition ?? null,
        })),
      );
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
      const node = nodes.find((candidate) => candidate.id === nodeId);
      if (!node) return;
      const deletedEdges = edges.filter(
        (edge) => edge.source === nodeId || edge.target === nodeId,
      );
      if (!confirmGraphDeletion([node], deletedEdges)) return;
      setNodes((ns) => ns.filter((n) => n.id !== nodeId));
      setEdges((es) => withEdgePresentations(es.filter((e) => e.source !== nodeId && e.target !== nodeId)));
      setEditingId(null);
    },
    [nodes, edges, setNodes, setEdges],
  );

  const patchEdge = useCallback(
    (edgeId: string, patch: Partial<AdvancedEdgeData>) => {
      setEdges((current) =>
        withEdgePresentations(current.map((edge) =>
          edge.id === edgeId
            ? {
                ...edge,
                data: {
                  kind: edgeKindOf(edge),
                  enabled: true,
                  ...(edge.data as AdvancedEdgeData | undefined),
                  ...patch,
                } satisfies AdvancedEdgeData,
              }
            : edge,
        )),
      );
    },
    [setEdges],
  );

  const deleteEdge = useCallback(
    (edgeId: string) => {
      const edge = edges.find((candidate) => candidate.id === edgeId);
      if (!edge || !confirmGraphDeletion([], [edge])) return;
      setEdges((current) => withEdgePresentations(current.filter((candidate) => candidate.id !== edgeId)));
      setEditingEdgeId(null);
    },
    [edges, setEdges],
  );

  const deleteSelection = useCallback(() => {
    const selectedNodes = nodes.filter((node) => node.selected);
    const selectedEdgeIds = new Set(edges.filter((edge) => edge.selected).map((edge) => edge.id));
    if (selectedNodes.length === 0 && selectedEdgeIds.size === 0) return;
    const nodeIds = new Set(selectedNodes.map((node) => node.id));
    const deletedEdges = edges.filter(
      (edge) =>
        selectedEdgeIds.has(edge.id) ||
        nodeIds.has(edge.source) ||
        nodeIds.has(edge.target),
    );
    if (!confirmGraphDeletion(selectedNodes, deletedEdges)) return;
    setNodes((current) => current.filter((node) => !nodeIds.has(node.id)));
    setEdges((current) =>
      current.filter(
        (edge) =>
          !selectedEdgeIds.has(edge.id) &&
          !nodeIds.has(edge.source) &&
          !nodeIds.has(edge.target),
      ),
    );
    setEditingId(null);
  }, [nodes, edges, setNodes, setEdges]);

  const saveNow = useCallback(async () => {
    // Collapse concurrent saves: if one is in flight, remember to run again
    // with the latest state once it settles.
    if (saveInFlightRef.current) {
      savePendingRef.current = true;
      return false;
    }
    saveInFlightRef.current = true;
    setSaveState("saving");
    setError(null);
    try {
      await updateWorkflow(id, {
        name,
        // "" clears the entrypoint server-side (null would mean "no change").
        entry_step: entryStep,
        execution_settings: executionSettings,
        nodes: toStudioNodes(nodes),
        edges: toStudioEdges(edges),
        viewport: rf ? rf.getViewport() : { x: 0, y: 0, zoom: 1 },
      });
      lastSavedSigRef.current = draftSig;
      if (draftSig === draftSigRef.current) {
        setSavedAt(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
        setSaveState("saved");
      } else {
        setSaveState("dirty");
      }
      return true;
    } catch (e) {
      setError(errMsg(e));
      setSaveState("dirty");
      return false;
    } finally {
      saveInFlightRef.current = false;
      if (savePendingRef.current) {
        savePendingRef.current = false;
        void saveNowRef.current();
      }
    }
  }, [id, name, entryStep, executionSettings, nodes, edges, rf, draftSig]);
  saveNowRef.current = saveNow;

  useEffect(() => {
    if (!navigationBlocked) return;
    const warn = (event: BeforeUnloadEvent) => {
      if (continueHistoryRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [navigationBlocked]);

  useEffect(() => {
    if (!navigationBlocked) return;
    const href = window.location.href;
    pushHistoryGuard(href);
    const guardNavigation = (event: MouseEvent) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      const target = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (
        !(target instanceof HTMLAnchorElement) ||
        target.target === "_blank" ||
        target.hasAttribute("download") ||
        target.getAttribute("href")?.startsWith("#") ||
        confirmNavigation(navigationBlocked)
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
    };
    const guardHistoryNavigation = () => {
      if (continueHistoryRef.current) {
        continueHistoryRef.current = false;
        return;
      }
      if (confirmNavigation(navigationBlocked)) {
        continueHistoryRef.current = true;
        window.history.back();
      } else {
        pushHistoryGuard(href);
      }
    };
    document.addEventListener("click", guardNavigation, true);
    window.addEventListener("popstate", guardHistoryNavigation);
    return () => {
      document.removeEventListener("click", guardNavigation, true);
      window.removeEventListener("popstate", guardHistoryNavigation);
      if (
        window.location.href === href &&
        window.history.state?.[HISTORY_GUARD_KEY] === true
      ) {
        window.history.back();
      }
    };
  }, [navigationBlocked]);

  // Debounced autosave (drafts only): any structural, name, or entrypoint
  // change after the initial load schedules a save 1.5s after the last change.
  useEffect(() => {
    if (!loadedRef.current || readOnly) return;
    if (draftSig === lastSavedSigRef.current) return;
    setSaveState("dirty");
    const t = window.setTimeout(() => void saveNowRef.current(), 1500);
    return () => window.clearTimeout(t);
  }, [draftSig, readOnly]);

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
          parallelConfig?: ParallelConfig | null;
          joinConfig?: JoinConfig | null;
        };
        return {
          id: n.id,
          studioType: d.studioType,
          label: d.label,
          config: d.config,
          inputContractRef: d.inputContractRef ?? null,
          outputContractRef: d.outputContractRef ?? null,
          parallelConfig: d.parallelConfig ?? null,
          joinConfig: d.joinConfig ?? null,
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
          data: {
            kind: edgeKindOf(e),
            condition: (e.data as AdvancedEdgeData | undefined)?.condition ?? null,
            mapping: (e.data as AdvancedEdgeData | undefined)?.mapping ?? null,
            enabled: (e.data as AdvancedEdgeData | undefined)?.enabled ?? true,
          },
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
    const pasted: Node[] = src.map((n) => withNodeEvidence({
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
        parallelConfig: n.parallelConfig ?? null,
        joinConfig: n.joinConfig ?? null,
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
        ...edgeKindProps(edgeKindOf({ sourceHandle: e.sourceHandle, data: e.data })),
        data: e.data ?? {
          kind: edgeKindOf({ sourceHandle: e.sourceHandle }),
          enabled: true,
        },
      }));
    // Pasted nodes become the selection so they can be dragged right away.
    setNodes((ns) => ns.map((n) => (n.selected ? { ...n, selected: false } : n)).concat(pasted));
    setEdges((es) => withEdgePresentations(es.concat(pastedEdges)));
  }, [readOnly, palette, setNodes, setEdges]);

  // Document-level shortcuts; the focus guard leaves native text editing
  // (including its own undo) alone inside form fields.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const primaryModifier = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();
      if (
        primaryModifier &&
        !e.shiftKey &&
        !e.altKey &&
        key === "s"
      ) {
        e.preventDefault();
        if (!readOnly && !e.repeat) void saveNow();
        return;
      }

      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      )
        return;
      if (
        key === "a" &&
        !primaryModifier &&
        !e.shiftKey &&
        !e.altKey &&
        !e.repeat &&
        !readOnly &&
        document.querySelector('[role="dialog"][aria-modal="true"]') === null
      ) {
        e.preventDefault();
        toggleNodeMenu();
        return;
      }
      if (!readOnly && (e.key === "Backspace" || e.key === "Delete")) {
        e.preventDefault();
        deleteSelection();
        return;
      }
      if (!primaryModifier) return;
      const k = key;
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
  }, [readOnly, undo, redo, copySelection, pasteClipboard, deleteSelection, saveNow, toggleNodeMenu]);

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

  // Publish validates against the *saved* draft, so flush the debounced
  // autosave first (including a queued follow-up save) or stale state gets
  // validated.
  async function publish() {
    setPublishing(true);
    setError(null);
    setPublishIssues(null);
    try {
      const published = await publishSavedDraft(
        async () => {
          while (saveInFlightRef.current || savePendingRef.current) {
            await new Promise((r) => window.setTimeout(r, 100));
          }
          do {
            if (!(await saveNowRef.current())) return false;
            while (saveInFlightRef.current || savePendingRef.current) {
              await new Promise((r) => window.setTimeout(r, 100));
            }
          } while (lastSavedSigRef.current !== draftSigRef.current);
          return true;
        },
        () => publishWorkflow(id),
      );
      if (!published) return;
      await load(); // status flips to published; editor becomes read-only
    } catch (e) {
      const issues = publishIssuesOf(e);
      if (issues) setPublishIssues(issues);
      else setError(errMsg(e));
    } finally {
      setPublishing(false);
    }
  }

  async function runPreflight() {
    setPreflighting(true);
    setError(null);
    setPublishIssues(null);
    try {
      while (saveInFlightRef.current || savePendingRef.current) {
        await new Promise((resolve) => window.setTimeout(resolve, 100));
      }
      if (!readOnly && !(await saveNowRef.current())) return;
      while (saveInFlightRef.current || savePendingRef.current) {
        await new Promise((resolve) => window.setTimeout(resolve, 100));
      }
      const result = await preflightWorkflow(id);
      setPreflight(result);
      if (!result.ready) {
        setPublishIssues(
          result.issues.map((issue) => ({
            ...issue,
            node_id: issue.node_id ?? null,
            edge_id: issue.edge_id ?? null,
          })),
        );
      }
    } catch (preflightError) {
      setError(errMsg(preflightError));
    } finally {
      setPreflighting(false);
    }
  }

  async function verifyProviders() {
    if (
      !window.confirm(
        "Call each distinct agent model with a tiny live prompt? This uses configured credentials, sends data to external providers, and may incur a small charge (maximum 3 models, 15 seconds each).",
      )
    ) return;
    setVerifyingProviders(true);
    setError(null);
    try {
      const result = await verifyWorkflowProviders(id);
      setLiveVerification(result);
      if (!result.verified) {
        setError(
          `Live verification failed: ${result.probes.filter((probe) => !probe.ok).map((probe) => `${probe.model} (${probe.error_code ?? "error"})`).join(", ")}`,
        );
      }
    } catch (verificationError) {
      setError(errMsg(verificationError));
    } finally {
      setVerifyingProviders(false);
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
          studioType: (target?.data as { studioType?: string })?.studioType,
          config: (target?.data as { config?: Cfg })?.config,
        };
      });
  }, [editing, edges, nodes]);

  // Full-bleed canvas (n8n-style): follow the global navigation rail as it
  // folds, so the workspace never leaves a dead gutter behind.
  return (
    <div
      ref={paneRef}
      className={`studio-editor-shell fixed bottom-0 right-0 top-0${placementType ? " is-placing-node" : ""}`}
      // Inline so the pane is sized from the very first paint — React Flow
      // refuses to render inside a parent without dimensions (rf-error 004),
      // and a fixed box with `left:auto` shrink-wraps to zero width until the
      // stylesheet lands. The ≤900px override still wins via !important.
      style={{ left: "var(--console-sidebar-width, 216px)" }}
      onPointerMove={trackPlacement}
    >
      <h1 className="sr-only">Workflow editor</h1>
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
          edgeTypes={edgeTypes}
          nodesFocusable
          deleteKeyCode={null}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={selectNode}
          // Preserve the click/configuration contract when a pointer moves a few
          // pixels between press and release (common on trackpads and touch).
          nodeClickDistance={6}
          nodeDragThreshold={4}
          onPaneClick={placeNode}
          isValidConnection={isValidConnection}
          onInit={setRf}
          onError={(code, message) => console.error("[rf-error]", code, message)}
          // A single click is the configuration contract. Keep the explicit
          // double-click handler for canvas-library and legacy compatibility.
          onNodeDoubleClick={(_, node) => setEditingId(node.id)}
          onEdgeDoubleClick={(_, edge) => setEditingEdgeId(edge.id)}
          selectionOnDrag
          panOnDrag={[1, 2]}
          panOnScroll
          fitView={initialViewportRef.current === null}
          fitViewOptions={{
            maxZoom: 1,
            padding: 0.18,
          }}
          defaultViewport={initialViewportRef.current ?? undefined}
          onMoveEnd={(_, viewport) => {
            try {
              window.sessionStorage.setItem(
                viewportStorageKey(id),
                JSON.stringify(viewport),
              );
            } catch {
              /* storage unavailable — camera persistence is best-effort */
            }
          }}
          minZoom={0.3}
          defaultEdgeOptions={{ markerEnd: { type: MarkerType.ArrowClosed } }}
          proOptions={{ hideAttribution: true }}
        >
          {/* Faint token-tinted dots: enough texture to read pan/zoom motion
              without competing with the graph (transparent dots rendered the
              component invisible — no spatial reference at all). */}
          <Background
            variant={BackgroundVariant.Dots}
            gap={22}
            size={1}
            color="var(--hair-strong)"
          />
          {placementType && (
            <div
              ref={placementGhostRef}
              className="studio-placement-ghost"
              role="status"
              aria-live="polite"
            >
              <span className="studio-placement-glyph">
                <NodeGlyph type={placementType.type} className="h-4 w-4" />
              </span>
              <span className="studio-placement-copy">
                <strong>Place {placementType.label}</strong>
                <span>Click to place · Esc cancels</span>
              </span>
            </div>
          )}
          {/* Navigation and authoring are separate groups: React Flow owns
              zoom/fit, while graph mutations live in the adjacent commandbar. */}
          <Controls orientation="horizontal" />
          <Panel position="bottom-left" className="studio-canvas-command-panel">
            <div className="studio-canvas-commandbar" aria-label="Canvas actions">
              <div ref={nodeMenuRef} className="studio-node-menu nodrag nopan">
                <button
                  type="button"
                  className="studio-canvas-command"
                  onClick={toggleNodeMenu}
                  disabled={readOnly}
                  aria-label="Add node"
                  aria-haspopup="menu"
                  aria-expanded={nodeMenuOpen}
                  aria-controls="studio-node-palette"
                  aria-keyshortcuts="A"
                  data-evidence-id="studio.canvas.add-node"
                  data-tooltip="Add node · A"
                >
                  <svg aria-hidden viewBox="0 0 16 16"><path d="M8 3v10M3 8h10" /></svg>
                  <span className="sr-only">Add node</span>
                </button>
                {nodeMenuOpen && (
                  <div
                    id="studio-node-palette"
                    className="studio-node-menu-popover"
                    role="menu"
                    aria-label="Node types"
                  >
                    {/* The "imported" category is registered so the canvas
                        can resolve its ports -- an imported MCP tool would
                        otherwise draw with no handles and its tool edge would not
                        attach -- but such a node is produced by import rather than
                        by placing it from here. It rides on the existing category
                        field because NodeTypeResponse's signature is pinned as an
                        immutable legacy capability and may not gain one. */}
                    {palette
                      .filter((type) => type.category !== IMPORTED_CATEGORY)
                      .map((type) => (
                        <button
                          key={type.type}
                          type="button"
                          role="menuitem"
                          className="studio-node-option"
                          onClick={() => beginPlacement(type)}
                        >
                          <span className="studio-node-option-glyph">
                            <NodeGlyph type={type.type} className="h-4 w-4" />
                          </span>
                          <span className="studio-node-option-copy">
                            <strong>{type.label}</strong>
                            <span>{NODE_META[type.type]?.blurb ?? NODE_META[type.type]?.help ?? type.category}</span>
                          </span>
                        </button>
                      ))}
                  </div>
                )}
              </div>
              {!readOnly && (
                <>
                  <button
                    type="button"
                    className="studio-canvas-command"
                    onClick={undo}
                    disabled={!histState.canUndo}
                    aria-label="Undo"
                    aria-keyshortcuts="Control+Z Meta+Z"
                    data-evidence-id="studio.canvas.undo"
                    data-tooltip="Undo · ⌘ Z"
                  >
                    <svg aria-hidden viewBox="0 0 16 16"><path d="M6 5H3v-3M3.5 5A5.5 5.5 0 1 1 3 11" /></svg>
                    <span className="sr-only">Undo</span>
                  </button>
                  <button
                    type="button"
                    className="studio-canvas-command"
                    onClick={redo}
                    disabled={!histState.canRedo}
                    aria-label="Redo"
                    aria-keyshortcuts="Control+Shift+Z Meta+Shift+Z"
                    data-evidence-id="studio.canvas.redo"
                    data-tooltip="Redo · ⇧ ⌘ Z"
                  >
                    <svg aria-hidden viewBox="0 0 16 16"><path d="M10 5h3v-3M12.5 5A5.5 5.5 0 1 0 13 11" /></svg>
                    <span className="sr-only">Redo</span>
                  </button>
                  {nodes.length > 1 && (
                    <button
                      type="button"
                      className="studio-canvas-command"
                      onClick={tidyLayout}
                      aria-label="Tidy layout"
                      data-evidence-id="studio.canvas.tidy"
                      data-tooltip="Tidy layout"
                    >
                      <svg aria-hidden viewBox="0 0 16 16"><rect x="2.5" y="3" width="4" height="3" /><rect x="9.5" y="10" width="4" height="3" /><path d="M6.5 4.5h3v7" /></svg>
                      <span className="sr-only">Tidy layout</span>
                    </button>
                  )}
                  <button
                    type="button"
                    className="studio-canvas-command"
                    onClick={saveNow}
                    disabled={saveState === "saving"}
                    aria-label="Save"
                    aria-keyshortcuts="Control+S Meta+S"
                    data-evidence-id="studio.canvas.save"
                    data-tooltip="Save · ⌘ S"
                  >
                    <svg aria-hidden viewBox="0 0 16 16"><path d="M3 2.5h8l2 2V13.5H3z" /><path d="M5 2.5v4h6v-3M5 13.5v-4h6v4" /></svg>
                    <span className="sr-only">Save</span>
                  </button>
                </>
              )}
              <button
                type="button"
                className="studio-canvas-command"
                onClick={runPreflight}
                disabled={preflighting || saveState === "saving"}
                aria-label="Run preflight"
                data-evidence-id="studio.canvas.preflight"
                data-tooltip="Run preflight"
              >
                <svg aria-hidden viewBox="0 0 16 16"><path d="m3 8 3 3 7-7" /></svg>
                <span className="sr-only">{preflighting ? "Preflighting…" : "Run preflight"}</span>
              </button>
            </div>
          </Panel>
          <div
            className="studio-editor-chrome pointer-events-none absolute inset-x-0 top-0 z-[4] h-[54px]"
          />

          {/* Floating toolbar: graph identity stays separate from node creation. */}
          <Panel position="top-left" className="studio-editor-left-panel">
            {/* Sized by content so short/empty names keep the status pills
                hugging the title instead of orphaning them mid-canvas; media
                rules cap max-width before the right cluster. */}
            <div className="studio-editor-left-stack pointer-events-none w-fit max-w-full space-y-4">
              <div
                className="studio-editor-titlebar pointer-events-auto flex h-[38px] items-center gap-2"
              >
                {/* Clearing the remembered id here keeps the list reachable:
                    the nav's Studio link otherwise points back at this editor. */}
                <Link
                  href="/studio"
                  onClick={() => setLastWorkflowId(null)}
                  className="studio-editor-back"
                  data-evidence-id="studio.editor.back-to-list"
                >
                  <svg aria-hidden viewBox="0 0 12 12">
                    <path d="M8.5 2 4.5 6l4 4" />
                  </svg>
                  <span className="sr-only">← Studio</span>
                </Link>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={readOnly}
                  aria-label="Workflow name"
                  placeholder="Untitled workflow"
                  className="studio-workflow-name min-w-[76px] max-w-[240px] border border-transparent bg-transparent px-1.5 py-0.5 hover:border-[var(--hair-strong)] focus-visible:border-[var(--accent)] disabled:opacity-100"
                  // Grows with the text (field-sizing browsers), so the status
                  // pills stay attached to the name instead of a fixed slot.
                  style={{ fieldSizing: "content" } as CSSProperties}
                />
                {/* State pill + version — the graph's lifecycle at a glance. */}
                <div className="studio-workflow-status flex items-center gap-2">
                  <Pill tone={status === "published" ? "success" : "muted"}>
                    {status || "draft"}
                  </Pill>
                  <ConsoleMeta className="studio-workflow-version">
                    v{version}
                  </ConsoleMeta>
                </div>
                {!readOnly && (
                  <ConsoleMeta aria-live="polite" className="studio-save-state">
                    {saveState === "saving"
                      ? "Saving…"
                      : saveState === "dirty"
                        ? "Unsaved changes"
                        : savedAt
                          ? `Saved ${savedAt}`
                          : ""}
                  </ConsoleMeta>
                )}
                <div className="sr-only" aria-label="Workflow verification states">
                  <Pill tone={status === "published" ? "success" : "muted"}>
                    {status === "published" ? "Published" : "Not published"}
                  </Pill>
                  <Pill tone={preflight?.ready ? "success" : preflight ? "danger" : "muted"}>
                    {preflight?.ready ? "Preflight passed" : preflight ? "Preflight failed" : "Not preflighted"}
                  </Pill>
                  <Pill tone={liveVerification?.verified ? "success" : liveVerification ? "danger" : "muted"}>
                    {liveVerification?.verified ? "Live provider verified" : liveVerification ? "Live provider failed" : "Live provider not verified"}
                  </Pill>
                </div>
                {/* Read-only banner (amber tint) for published/immutable graphs;
                    routine draft editing gets a neutral note. */}
                {readOnly ? (
                  <div
                    className="sr-only"
                    style={{
                      background: "rgba(252,211,77,0.10)",
                      border: "1px solid rgba(252,211,77,0.30)",
                      borderRadius: 6,
                      padding: "6px 8px",
                      fontFamily: "var(--font-mono)",
                      fontSize: 11.5,
                      lineHeight: 1.45,
                      color: "var(--warning)",
                    }}
                  >
                    read-only — published graphs are immutable; clone to a draft to edit
                  </div>
                ) : (
                  <p className="sr-only" style={{ color: "var(--text-muted)" }}>
                    Draft — nodes, edges, config &amp; layout save here. Fields marked{" "}
                    <span className="font-semibold">*</span> are required to publish.
                  </p>
                )}
              </div>

            </div>
          </Panel>

          {/* Lifecycle stays quiet: one primary action and one overflow. */}
          <Panel position="top-right" className="studio-editor-right-panel">
            <div className="studio-editor-right-stack flex flex-col items-end gap-2">
              <div className="studio-workflow-actions">
                <div ref={workflowMenuRef} className="studio-actions-menu">
                  <button
                    type="button"
                    className="studio-more-actions"
                    onClick={() => setWorkflowMenuOpen((open) => !open)}
                    aria-haspopup="menu"
                    aria-expanded={workflowMenuOpen}
                    aria-controls="studio-workflow-actions-menu"
                    data-evidence-id="studio.workflow.more-actions"
                  >
                    More
                    <svg aria-hidden viewBox="0 0 12 12"><circle cx="2" cy="6" r="1" /><circle cx="6" cy="6" r="1" /><circle cx="10" cy="6" r="1" /></svg>
                  </button>
                  {workflowMenuOpen && (
                    <div
                      id="studio-workflow-actions-menu"
                      className="studio-actions-menu-popover"
                      role="menu"
                      aria-label="Workflow actions"
                    >
                    <button
                      type="button"
                      className="studio-menu-action"
                      role="menuitem"
                      onClick={() => { setWorkflowMenuOpen(false); setLoopSafetyOpen(true); }}
                    >
                      Loop safety
                    </button>
                    <button
                      type="button"
                      className="studio-menu-action"
                      role="menuitem"
                      onClick={() => { setWorkflowMenuOpen(false); void verifyProviders(); }}
                      disabled={!preflight?.ready || verifyingProviders}
                    >
                      {verifyingProviders ? "Verifying…" : "Verify providers"}
                    </button>
                    {version > 1 && (
                      <button type="button" role="menuitem" className="studio-menu-action" onClick={() => { setWorkflowMenuOpen(false); setHistoryOpen(true); }}>
                        Version history
                      </button>
                    )}
                    {canDeployWorkflow(status) && (
                      <button
                        type="button"
                        role="menuitem"
                        className="studio-menu-action"
                        onClick={() => { setWorkflowMenuOpen(false); void clone(); }}
                        disabled={cloning}
                        data-evidence-id="studio.workflow.clone-to-draft"
                      >
                        {cloning ? "Cloning…" : "Clone to draft"}
                      </button>
                    )}
                  </div>
                  )}
                </div>
                {canDeployWorkflow(status) ? (
                  <PButton variant="primary" onClick={() => setDeployOpen(true)} data-evidence-id="studio.workflow.deploy">
                    Deploy
                  </PButton>
                ) : (
                  <PButton
                    variant="primary"
                    onClick={publish}
                    disabled={publishing || nodes.length === 0}
                    data-evidence-id="studio.workflow.publish"
                  >
                    {publishing ? "Publishing…" : "Publish"}
                  </PButton>
                )}
              </div>
              {copied !== null && (
                <ConsoleMeta className="studio-copy-state">
                  Copied {copied} node{copied === 1 ? "" : "s"}
                </ConsoleMeta>
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
              {others.length > 0 && (
                <div
                  className="hidden"
                  style={{
                    background: "var(--bg-raised)",
                    border: "1px solid var(--hair-strong)",
                    borderRadius: 8,
                    boxShadow: "0 4px 16px rgba(0,0,0,0.35)",
                    transform: `translate(${wfOffset.x}px, ${wfOffset.y}px)`,
                  }}
                >
                  <div className="flex items-center">
                    {/* Grip: drag the widget anywhere on the canvas. */}
                    <span
                      onPointerDown={onWfPointerDown}
                      onPointerMove={onWfPointerMove}
                      onPointerUp={onWfPointerUp}
                      title="Drag to move"
                      aria-hidden
                      className="touch-none cursor-grab select-none py-2 pl-3 pr-1 active:cursor-grabbing"
                      style={{ color: "var(--text-faint)" }}
                    >
                      ⠿
                    </span>
                    <button
                      onClick={() => setSwitcherOpen((o) => !o)}
                      aria-expanded={switcherOpen}
                      className="flex flex-1 items-center justify-between py-2 pl-1 pr-3"
                      style={{
                        fontFamily: "var(--font-mono)",
                        fontSize: 12.5,
                        fontWeight: 600,
                        color: "var(--text-secondary)",
                      }}
                    >
                      Workflows ({others.length})
                      <span
                        aria-hidden
                        className={`transition-transform ${switcherOpen ? "rotate-180" : ""}`}
                        style={{ fontSize: 11, color: "var(--text-faint)" }}
                      >
                        ▾
                      </span>
                    </button>
                  </div>
                  {switcherOpen && (
                    <div
                      className="max-h-[45vh] space-y-0.5 overflow-y-auto p-1.5"
                      style={{ borderTop: "1px solid var(--hair)" }}
                    >
                      {others.map((w) => (
                        <Link
                          key={w.id}
                          href={`/studio/edit?id=${encodeURIComponent(w.id)}`}
                          data-evidence-id={`studio.workflow-switcher.${w.id}`}
                          className="flex items-center justify-between gap-2 border border-transparent px-2 py-1.5 text-sm transition-colors hover:border-[var(--hair-strong)] hover:bg-[var(--bg-raised-2)]"
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

          {nodes.length === 0 && (
            <Panel position="top-center" className="pointer-events-none">
              <div className="studio-empty-canvas">
                Use Add node, choose a type, then place it on the canvas.
                {!readOnly && (
                  <div className="pointer-events-auto mt-2">
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
          templates={templates}
          templateAccessError={templateAccessError}
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

      {edges.find((edge) => edge.id === editingEdgeId) && (
        <EdgeEditorDialog
          edge={edges.find((edge) => edge.id === editingEdgeId)!}
          readOnly={readOnly}
          onClose={() => setEditingEdgeId(null)}
          onPatch={(patch) => patchEdge(editingEdgeId!, patch)}
          onDelete={() => deleteEdge(editingEdgeId!)}
        />
      )}

      {loopSafetyOpen && (
        <LoopSafetyDialog
          settings={executionSettings}
          readOnly={readOnly}
          onClose={() => setLoopSafetyOpen(false)}
          onApply={(next) => {
            setExecutionSettings(next);
            setLoopSafetyOpen(false);
          }}
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

function LoopSafetyDialog({
  settings,
  readOnly,
  onClose,
  onApply,
}: {
  settings: StudioExecutionSettings;
  readOnly: boolean;
  onClose: () => void;
  onApply: (settings: StudioExecutionSettings) => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [draft, setDraft] = useState(settings);

  useEffect(() => closeRef.current?.focus(), []);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);

  const setRequired = (key: "max_total_steps" | "max_visits_per_node", value: string) => {
    const parsed = Number(value);
    if (Number.isInteger(parsed) && parsed >= 1) setDraft((current) => ({ ...current, [key]: parsed }));
  };
  const setOptional = (
    key: "max_total_runtime_seconds" | "max_visits_per_edge" | "default_timeout_seconds",
    value: string,
  ) => {
    if (value === "") {
      setDraft((current) => ({ ...current, [key]: null }));
      return;
    }
    const parsed = Number(value);
    if (Number.isInteger(parsed) && parsed >= 1) setDraft((current) => ({ ...current, [key]: parsed }));
  };

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-20" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Loop safety limits"
        className="w-full max-w-xl rounded-lg border border-border bg-surface shadow-md shadow-black/[0.08]"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <div className="text-sm font-semibold">Loop safety</div>
            <div className="text-xs text-muted">Fail-closed graph execution limits</div>
          </div>
          <Button ref={closeRef} variant="ghost" size="sm" onClick={onClose} aria-label="Close">✕</Button>
        </header>
        <div className="space-y-4 p-4">
          <ConsoleNotice tone="neutral">
            Loop nodes own Repeat, Done, and Limit routing. Set each loop&apos;s exit expression and
            maximum retries on the node; these graph-wide limits remain the final fail-closed guard.
          </ConsoleNotice>
          <div className="grid grid-cols-2 gap-3 max-[560px]:grid-cols-1">
            <label className="text-xs font-medium">
              Maximum total steps
              <input
                aria-label="Maximum total steps"
                className={compactInput}
                type="number"
                min={1}
                value={draft.max_total_steps}
                disabled={readOnly}
                onChange={(event) => setRequired("max_total_steps", event.target.value)}
              />
              <span className="mt-1 block font-normal text-muted">All node executions in one run.</span>
            </label>
            <label className="text-xs font-medium">
              Maximum runtime (seconds)
              <input
                aria-label="Maximum total runtime seconds"
                className={compactInput}
                type="number"
                min={1}
                value={draft.max_total_runtime_seconds ?? ""}
                placeholder="No additional limit"
                disabled={readOnly}
                onChange={(event) => setOptional("max_total_runtime_seconds", event.target.value)}
              />
              <span className="mt-1 block font-normal text-muted">Wall-clock ceiling for the run.</span>
            </label>
            <label className="text-xs font-medium">
              Maximum visits per node
              <input
                aria-label="Maximum visits per node"
                className={compactInput}
                type="number"
                min={1}
                value={draft.max_visits_per_node}
                disabled={readOnly}
                onChange={(event) => setRequired("max_visits_per_node", event.target.value)}
              />
              <span className="mt-1 block font-normal text-muted">Bounds repeated work at each step.</span>
            </label>
            <label className="text-xs font-medium">
              Maximum visits per edge
              <input
                aria-label="Maximum visits per edge"
                className={compactInput}
                type="number"
                min={1}
                value={draft.max_visits_per_edge ?? ""}
                placeholder="Use condition + graph limits"
                disabled={readOnly}
                onChange={(event) => setOptional("max_visits_per_edge", event.target.value)}
              />
              <span className="mt-1 block font-normal text-muted">Optional route-specific backstop.</span>
            </label>
          </div>
        </div>
        <footer className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <Button variant="default" onClick={onClose}>{readOnly ? "Close" : "Cancel"}</Button>
          {!readOnly && <Button variant="primary" onClick={() => onApply(draft)}>Apply limits</Button>}
        </footer>
      </div>
    </div>
  );
}

function newMappingOperation(operation: EdgeMappingOperation["operation"]): EdgeMappingOperation {
  if (operation === "constant") return { operation, target_path: "", value: "" };
  if (operation === "default") {
    return { operation, source_path: null, target_path: "", default_value: "" };
  }
  if (operation === "transform") return { operation, target_path: "", expression: "" };
  return { operation, source_path: "", target_path: "" };
}

function EdgeEditorDialog({
  edge,
  readOnly,
  onClose,
  onPatch,
  onDelete,
}: {
  edge: Edge;
  readOnly: boolean;
  onClose: () => void;
  onPatch: (patch: Partial<AdvancedEdgeData>) => void;
  onDelete: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const data = {
    kind: edgeKindOf(edge),
    enabled: true,
    ...(edge.data as AdvancedEdgeData | undefined),
  };
  const condition = data.condition ?? null;
  const operations = data.mapping?.operations ?? [];

  useEffect(() => closeRef.current?.focus(), []);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onClose]);

  const setOperations = (next: EdgeMappingOperation[]) =>
    onPatch({ mapping: next.length > 0 ? { operations: next } : null });

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4 pt-20" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Edit edge ${edge.id}`}
        className="w-full max-w-2xl rounded-lg border border-border bg-surface shadow-md shadow-black/[0.08]"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-border px-4 py-3">
          <div>
            <div className="text-sm font-semibold">Execution edge</div>
            <div className="font-mono text-[10px] text-muted">{edge.source} → {edge.target}</div>
          </div>
          <Button ref={closeRef} variant="ghost" size="sm" onClick={onClose} aria-label="Close">✕</Button>
        </header>
        <div className="max-h-[65vh] space-y-5 overflow-auto p-4">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={data.enabled}
              disabled={readOnly}
              onChange={(event) => onPatch({ enabled: event.target.checked })}
            />
            Edge enabled
          </label>

          {condition && (
            <section
              className="border-t border-border pt-4"
              data-evidence-id="studio.edge.legacy-condition"
            >
              <p className="text-xs text-muted">
                This legacy conditional route is preserved for compatibility. Use an If or Loop
                node to author or change routing logic.
              </p>
            </section>
          )}

          <section className="space-y-3 border-t border-border pt-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold">Payload mapping</h3>
                <p className="text-xs text-muted">Build ordered field operations without replacing the entire edge as JSON.</p>
              </div>
              {!readOnly && (
                <select
                  aria-label="Add mapping operation"
                  className="rounded-md border border-border bg-background px-2 py-1.5 text-xs"
                  value=""
                  onChange={(event) => {
                    if (!event.target.value) return;
                    setOperations([...operations, newMappingOperation(event.target.value as EdgeMappingOperation["operation"])]);
                    event.target.value = "";
                  }}
                >
                  <option value="">Add operation…</option>
                  <option value="passthrough">Passthrough</option>
                  <option value="rename">Rename</option>
                  <option value="constant">Constant</option>
                  <option value="default">Default</option>
                  <option value="transform">Transform</option>
                </select>
              )}
            </div>
            {operations.length === 0 ? (
              <p className="rounded-md border border-dashed border-border px-3 py-2 text-xs text-muted">Payload passes through unchanged.</p>
            ) : operations.map((operation, index) => (
              <MappingOperationEditor
                key={`${operation.operation}-${index}`}
                operation={operation}
                index={index}
                readOnly={readOnly}
                onChange={(next) => setOperations(operations.map((current, currentIndex) => currentIndex === index ? next : current))}
                onRemove={() => setOperations(operations.filter((_, currentIndex) => currentIndex !== index))}
              />
            ))}
          </section>
        </div>
        <footer className="flex items-center justify-between border-t border-border px-4 py-3">
          {readOnly ? <span className="text-xs text-muted">Read-only (published)</span> : (
            <Button variant="danger" size="sm" onClick={onDelete}>Delete edge</Button>
          )}
          <Button variant="primary" size="sm" onClick={onClose}>Done</Button>
        </footer>
      </div>
    </div>
  );
}

function MappingOperationEditor({
  operation,
  index,
  readOnly,
  onChange,
  onRemove,
}: {
  operation: EdgeMappingOperation;
  index: number;
  readOnly: boolean;
  onChange: (value: EdgeMappingOperation) => void;
  onRemove: () => void;
}) {
  const patch = (value: Record<string, unknown>) => onChange({ ...operation, ...value } as EdgeMappingOperation);
  return (
    <div className="space-y-2 rounded-lg border border-border p-3">
      <div className="flex items-center justify-between">
        <span className="font-mono text-[10px] font-semibold uppercase text-muted">{index + 1}. {operation.operation}</span>
        {!readOnly && <Button variant="ghost" size="sm" onClick={onRemove}>Remove</Button>}
      </div>
      <div className="grid grid-cols-2 gap-2">
        {"source_path" in operation && (
          <label className="text-xs font-medium">
            Source path
            <input className={compactInput} value={operation.source_path ?? ""} disabled={readOnly} onChange={(event) => patch({ source_path: event.target.value || (operation.operation === "default" ? null : "") })} />
          </label>
        )}
        <label className="text-xs font-medium">
          Target path
          <input className={compactInput} value={operation.target_path} disabled={readOnly} onChange={(event) => patch({ target_path: event.target.value })} />
        </label>
        {operation.operation === "transform" && (
          <label className="col-span-2 text-xs font-medium">
            Transform expression
            <input className={compactInput} value={operation.expression} disabled={readOnly} onChange={(event) => patch({ expression: event.target.value })} />
          </label>
        )}
        {(operation.operation === "constant" || operation.operation === "default") && (
          <label className="col-span-2 text-xs font-medium">
            {operation.operation === "constant" ? "Value" : "Default value"} (JSON literal)
            <input
              className={compactInput}
              value={JSON.stringify(operation.operation === "constant" ? operation.value : operation.default_value)}
              disabled={readOnly}
              onChange={(event) => {
                let value: unknown = event.target.value;
                try { value = JSON.parse(event.target.value); } catch { /* preserve in-progress text */ }
                patch(operation.operation === "constant" ? { value } : { default_value: value });
              }}
            />
          </label>
        )}
      </div>
    </div>
  );
}

function NodeEditorDialog({
  node,
  readOnly,
  connectors,
  templates,
  templateAccessError,
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
  templates: Template[];
  templateAccessError: string | null;
  onConnectorsChanged: () => void | Promise<void>;
  manifestRefs: string[];
  contractNames: string[];
  onContractsChanged?: () => void | Promise<void>;
  /** Units attached to this node via tool edges (agent nodes only). */
  toolTargets?: { id: string; label: string; studioType?: string; config?: Cfg }[];
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
    parallelConfig?: ParallelConfig | null;
    joinConfig?: JoinConfig | null;
  };
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [tab, setTab] = useState<"config" | "execution" | "activity">("config");
  const dispatchableAsStep = d.studioType !== "mcp_tool";
  // Activity mounts lazily on first visit (its fetch runs on mount) and then
  // stays mounted-but-hidden so toggling tabs doesn't refetch.
  const [activityOpened, setActivityOpened] = useState(false);

  // Move focus into the dialog on open so keyboard users aren't left behind
  // on the canvas. Defer until the initiating pointer gesture is complete,
  // then restore focus to its invoking node when the dialog closes.
  useEffect(() => {
    const returnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const frame = window.requestAnimationFrame(() => closeRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      returnFocus?.focus();
    };
  }, []);

  useEffect(() => {
    function focusableDialogElements(dialog: HTMLElement) {
      return Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => {
        if (element.closest('[hidden], [aria-hidden="true"], .hidden')) return false;
        const style = window.getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden";
      });
    }

    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = focusableDialogElements(dialog);
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const active = document.activeElement as HTMLElement | null;
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!active || !dialog.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first?.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center overflow-y-auto bg-black/40 p-4"
      onMouseDown={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Edit ${d.label}`}
        tabIndex={-1}
        // Code nodes host a real editor — give them room to breathe.
        className={`flex max-h-[calc(100dvh-2rem)] w-full flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-md shadow-black/[0.08] ${
          d.studioType === "code" ? "max-w-2xl" : "max-w-md"
        }`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <span
              className="grid h-8 w-8 shrink-0 place-items-center"
              style={{ color: nodeMetaColor(d.studioType) }}
            >
              <NodeGlyph type={d.studioType} className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">{d.label || d.studioType}</div>
              <div
                className="truncate"
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 9,
                  fontWeight: 600,
                  letterSpacing: "0.09em",
                  textTransform: "uppercase",
                  color: "var(--text-muted)",
                }}
              >
                {d.studioType}
              </div>
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
          {/* An mcp_tool node is reached only through a tool edge from the agent
              that binds it -- publish rejects it as an entry step and rejects a
              data edge touching it -- so it is never dispatched as a graph step
              and nothing reads its parallel/join config. Offering the controls
              would invite an author to configure fan-out that can never run. */}
          {dispatchableAsStep && (
            <TabButton active={tab === "execution"} onClick={() => setTab("execution")}>
              Execution
            </TabButton>
          )}
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

        <div className="min-h-0 overflow-auto p-4">
          <div className={tab === "config" ? "" : "hidden"}>
            <NodeInspector
              studioType={d.studioType}
              label={d.label}
              config={d.config}
              nodeId={node.id}
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
              templates={templates}
              templateAccessError={templateAccessError}
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
          <div className={tab === "execution" && dispatchableAsStep ? "" : "hidden"}>
            <NodeExecutionSettings
              parallel={d.parallelConfig ?? null}
              join={d.joinConfig ?? null}
              readOnly={readOnly}
              onParallelChange={(parallelConfig) => onPatch({ parallelConfig })}
              onJoinChange={(joinConfig) => onPatch({ joinConfig })}
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
    </div>,
    document.body,
  );
}

const compactInput =
  "mt-1 w-full rounded-md border border-border bg-background px-2.5 py-2 text-sm text-foreground disabled:opacity-60";

function optionalPositive(raw: string): number | null {
  if (raw.trim() === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function NodeExecutionSettings({
  parallel,
  join,
  readOnly,
  onParallelChange,
  onJoinChange,
}: {
  parallel: ParallelConfig | null;
  join: JoinConfig | null;
  readOnly: boolean;
  onParallelChange: (value: ParallelConfig | null) => void;
  onJoinChange: (value: JoinConfig | null) => void;
}) {
  const mergeOptions = ["collect", "reduce", "merge", "custom"] as const;
  const patchParallel = (patch: Partial<ParallelConfig>) =>
    onParallelChange({ split_path: parallel?.split_path ?? "items", ...parallel, ...patch });
  const patchJoin = (patch: Partial<JoinConfig>) =>
    onJoinChange({ merge_strategy: "collect", ...join, ...patch });

  return (
    <div className="space-y-5">
      <section className="space-y-3">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={parallel !== null}
            disabled={readOnly}
            onChange={(event) =>
              onParallelChange(
                event.target.checked
                  ? { split_path: "items", merge_strategy: "collect", fail_mode: "fail_fast" }
                  : null,
              )
            }
          />
          <span>
            <strong className="block">Parallel fan-out and batching</strong>
            <span className="text-xs text-muted">
              Split a list into bounded branches, optionally processed in sequential waves.
            </span>
          </span>
        </label>
        {parallel && (
          <div className="grid grid-cols-2 gap-3 border-l border-border pl-3">
            <label className="col-span-2 text-xs font-medium">
              List path
              <input
                aria-label="Parallel list path"
                className={compactInput}
                value={parallel.split_path}
                disabled={readOnly}
                onChange={(event) => patchParallel({ split_path: event.target.value })}
              />
            </label>
            <label className="text-xs font-medium">
              Merge strategy
              <select
                aria-label="Parallel merge strategy"
                className={compactInput}
                value={parallel.merge_strategy ?? "collect"}
                disabled={readOnly}
                onChange={(event) => {
                  const merge_strategy = event.target.value as ParallelConfig["merge_strategy"];
                  patchParallel({
                    merge_strategy,
                    reducer_ref: merge_strategy === "custom" ? parallel.reducer_ref : null,
                  });
                }}
              >
                {mergeOptions.map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            <label className="text-xs font-medium">
              Failure mode
              <select
                aria-label="Parallel failure mode"
                className={compactInput}
                value={parallel.fail_mode ?? "fail_fast"}
                disabled={readOnly}
                onChange={(event) =>
                  patchParallel({ fail_mode: event.target.value as ParallelConfig["fail_mode"] })
                }
              >
                <option value="fail_fast">fail fast</option>
                <option value="best_effort">best effort</option>
              </select>
            </label>
            {parallel.merge_strategy === "custom" && (
              <label className="col-span-2 text-xs font-medium">
                Reducer import path
                <input
                  aria-label="Parallel reducer import path"
                  className={compactInput}
                  value={parallel.reducer_ref ?? ""}
                  disabled={readOnly}
                  onChange={(event) => patchParallel({ reducer_ref: event.target.value || null })}
                />
              </label>
            )}
            {([
              ["max_branches", "Maximum branches"],
              ["max_concurrency", "Maximum concurrency"],
              ["batch_size", "Batch size"],
              ["branch_timeout_seconds", "Branch timeout (seconds)"],
            ] as const).map(([key, label]) => (
              <label key={key} className="text-xs font-medium">
                {label}
                <input
                  aria-label={label}
                  type="number"
                  min="1"
                  step={key === "branch_timeout_seconds" ? "any" : "1"}
                  className={compactInput}
                  value={parallel[key] ?? ""}
                  disabled={readOnly}
                  onChange={(event) => patchParallel({ [key]: optionalPositive(event.target.value) })}
                />
              </label>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3 border-t border-border pt-4">
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={join !== null}
            disabled={readOnly}
            onChange={(event) =>
              onJoinChange(event.target.checked ? { merge_strategy: "collect", merge_path: "result" } : null)
            }
          />
          <span>
            <strong className="block">Join inbound paths</strong>
            <span className="text-xs text-muted">
              Define how payloads from multiple inbound execution paths combine.
            </span>
          </span>
        </label>
        {join && (
          <div className="space-y-3 border-l border-border pl-3">
            <label className="block text-xs font-medium">
              Merge strategy
              <select
                aria-label="Join merge strategy"
                className={compactInput}
                value={join.merge_strategy ?? "collect"}
                disabled={readOnly}
                onChange={(event) => {
                  const merge_strategy = event.target.value as JoinConfig["merge_strategy"];
                  patchJoin({ merge_strategy, reducer_ref: merge_strategy === "custom" ? join.reducer_ref : null });
                }}
              >
                {mergeOptions.map((value) => <option key={value}>{value}</option>)}
              </select>
            </label>
            {join.merge_strategy === "custom" && (
              <label className="block text-xs font-medium">
                Reducer import path
                <input
                  aria-label="Join reducer import path"
                  className={compactInput}
                  value={join.reducer_ref ?? ""}
                  disabled={readOnly}
                  onChange={(event) => patchJoin({ reducer_ref: event.target.value || null })}
                />
              </label>
            )}
            <label className="block text-xs font-medium">
              Output path
              <input
                aria-label="Join output path"
                className={compactInput}
                value={join.merge_path ?? ""}
                disabled={readOnly}
                onChange={(event) => patchJoin({ merge_path: event.target.value || null })}
              />
            </label>
          </div>
        )}
      </section>
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
// The run panel survives navigation: its working state (payload draft, thread,
// submitted run id) is kept per workflow in sessionStorage and restored on
// mount, so leaving the editor and coming back does not clear the run — the
// poll effect re-fetches the restored run and repaints the canvas overlay.
type StoredRunPanel = {
  runId?: string | null;
  threadId?: string;
  payload?: string;
  payloadSource?: "contract" | "user";
};

function runPanelStorageKey(workflowId: string): string {
  return `zeroth.studio.runPanel.${workflowId}`;
}

function readStoredRunPanel(workflowId: string): StoredRunPanel {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(window.sessionStorage.getItem(runPanelStorageKey(workflowId)) ?? "{}");
  } catch {
    return {};
  }
}

const DEFAULT_RUN_PAYLOAD = "{}";
const LEGACY_DEFAULT_RUN_PAYLOAD = '{\n  "question": "What is Zeroth?"\n}';

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
  const initialStored = useRef(readStoredRunPanel(workflowId)).current;
  const [open, setOpen] = useState(() => initialStored.runId != null);
  // undefined = health check in flight; null = unreachable/no deployment.
  const [servedRef, setServedRef] = useState<string | null | undefined>(undefined);
  const [servedDeploymentRef, setServedDeploymentRef] = useState<string | null>(null);
  const [servedCampaignId, setServedCampaignId] = useState<string | null>(null);
  const deployedId = servedRef == null ? servedRef : servedGraphId(servedRef);
  const [payload, setPayload] = useState(
    () => initialStored.payload ?? DEFAULT_RUN_PAYLOAD,
  );
  const storedPayloadIsLegacyDefault =
    initialStored.payload === LEGACY_DEFAULT_RUN_PAYLOAD;
  const payloadSourceRef = useRef<"contract" | "user" | "default">(
    storedPayloadIsLegacyDefault
      ? "default"
      : initialStored.payloadSource ??
          (initialStored.payload == null ? "default" : "user"),
  );
  // Conversation key sent as thread_id. Prefilled from each run's response so
  // repeated runs continue the same conversation; cleared = start fresh.
  const [threadId, setThreadId] = useState(() => initialStored.threadId ?? "");
  const [runId, setRunId] = useState<string | null>(
    () => initialStored.runId ?? null,
  );
  const [run, setRun] = useState<RunStatus | null>(null);
  const [failedNode, setFailedNode] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pastOpen, setPastOpen] = useState(false);
  const [past, setPast] = useState<RunStatus[] | null>(null);
  const [pastError, setPastError] = useState<string | null>(null);
  const [selectedPast, setSelectedPast] = useState<string | null>(null);
  const runSelectionRequestRef = useRef(0);
  // The poll tick reads node ids through a ref so nodes added/moved mid-run
  // are picked up without restarting the interval.
  const nodeIdsRef = useRef(nodeIds);
  nodeIdsRef.current = nodeIds;

  // Keep the stored panel state current (the editor remounts per workflow id,
  // so each workflow reads and writes only its own key).
  useEffect(() => {
    try {
      window.sessionStorage.setItem(
        runPanelStorageKey(workflowId),
        JSON.stringify({
          runId,
          threadId,
          payload,
          payloadSource:
            payloadSourceRef.current === "default" ? undefined : payloadSourceRef.current,
        } satisfies StoredRunPanel),
      );
    } catch {
      /* storage full/unavailable — persistence is best-effort */
    }
  }, [workflowId, runId, threadId, payload]);

  // POST /v1/runs always executes the deployment's graph, so only the
  // workflow whose id matches health.graph_version_ref ("graphId@version")
  // can be run from this canvas.
  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((h) => {
        if (!cancelled) {
          setServedRef(h.graph_version_ref);
          setServedDeploymentRef(h.deployment_ref);
          setServedCampaignId(h.campaign_id ?? null);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setServedRef(null);
          setServedDeploymentRef(null);
          setServedCampaignId(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!servedDeploymentRef || !canRunWorkflow(workflowId, servedRef)) return;
    let cancelled = false;
    getInputContract(servedDeploymentRef)
      .then((contract) => {
        if (cancelled || payloadSourceRef.current === "user") return;
        payloadSourceRef.current = "contract";
        setPayload(JSON.stringify(examplePayloadFromSchema(contract.json_schema), null, 2));
      })
      .catch(() => {
        // Keep the safe empty object when a legacy deployment has no schema.
      });
    return () => {
      cancelled = true;
    };
  }, [servedDeploymentRef, servedRef, workflowId]);

  const isDeployed = canRunWorkflow(workflowId, servedRef);
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
        if (!stopped) {
          if (e instanceof ApiError && e.status === 404) {
            // Restored run no longer exists (e.g. the database was reseeded)
            // — drop it silently instead of wedging the panel on an error.
            setRunId(null);
            setRun(null);
            setFailedNode(null);
            onStates({});
          } else {
            setError(errMsg(e));
          }
        }
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
    const request = ++runSelectionRequestRef.current;
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
      const res = await submitRun({
        input_payload: parsed,
        thread_id: threadId.trim() || undefined,
        campaign_id: servedCampaignId || undefined,
      });
      if (request !== runSelectionRequestRef.current) return;
      setRunId(res.run_id);
      // The service mints a thread for id-less runs — adopt it so the next
      // run from this panel continues the conversation.
      if (res.thread_id) setThreadId(res.thread_id);
    } catch (e) {
      if (request !== runSelectionRequestRef.current) return;
      setError(errMsg(e));
      onStates({});
    } finally {
      setSubmitting(false);
    }
  }

  function clear() {
    runSelectionRequestRef.current += 1;
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
    const request = ++runSelectionRequestRef.current;
    setRunId(null);
    setSelectedPast(r.run_id);
    setError(null);
    try {
      const [status, tl] = await Promise.all([
        getRun(r.run_id),
        getRunTimeline(r.run_id).catch(() => null),
      ]);
      if (request !== runSelectionRequestRef.current) return;
      const entries = tl?.entries ?? [];
      setRun(status);
      const failed = entries.filter((e) => entryPhase(e) === "failed");
      setFailedNode(failed.length > 0 ? failed[failed.length - 1].node_id : null);
      onStates(deriveNodeStates(entries));
    } catch (e) {
      if (request === runSelectionRequestRef.current) setError(errMsg(e));
    }
  }

  if (!open) {
    return (
      <Panel position="bottom-center" className="studio-run-dock">
        <button
          onClick={() => setOpen(true)}
          data-evidence-id="studio.run.open"
          className="flex items-center gap-2 rounded-lg border border-border bg-surface px-4 py-1.5 text-sm font-medium transition-colors hover:border-accent/40 hover:text-accent"
        >
          Run
          {run && <StatusBadge status={run.status} />}
        </button>
      </Panel>
    );
  }

  const deployedWf = others.find((w) => w.id === deployedId);

  return (
    <StudioDialog
      ariaLabel="Run workflow"
      onClose={() => setOpen(false)}
      evidenceId="studio.run.dialog"
      className="studio-run-panel studio-run-dialog"
    >
        <header className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
          <div className="flex items-center gap-2 text-sm font-semibold">
            Run
            {run && <StatusBadge status={run.status} />}
            {polling && <span className="text-xs font-normal text-muted">auto-refreshing…</span>}
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setOpen(false)}
            aria-label="Close run dialog"
            data-dialog-autofocus
            data-evidence-id="studio.run.close"
          >
            <svg aria-hidden viewBox="0 0 12 12" className="h-3 w-3">
              <path d="M2 2l8 8M10 2l-8 8" fill="none" stroke="currentColor" strokeWidth="1.5" />
            </svg>
          </Button>
        </header>

        <div className="max-h-[62vh] space-y-3 overflow-y-auto p-3">
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
                  onChange={(e) => {
                    payloadSourceRef.current = "user";
                    setPayload(e.target.value);
                  }}
                  rows={10}
                  className="min-h-[8rem] font-mono text-xs"
                  data-evidence-id="studio.run.input-payload"
                />
              </Field>
              <Field
                label="Thread"
                hint="conversation key — runs sharing it continue one conversation; blank starts fresh"
              >
                <div className="flex items-center gap-1.5">
                  <Input
                    value={threadId}
                    onChange={(e) => setThreadId(e.target.value)}
                    placeholder="(new conversation)"
                    className="font-mono text-xs"
                    aria-label="Thread"
                    data-evidence-id="studio.run.thread"
                  />
                  {threadId && (
                    <Button
                      size="sm"
                      onClick={() => setThreadId("")}
                      title="Start a new conversation"
                      data-evidence-id="studio.run.new-thread"
                    >
                      New
                    </Button>
                  )}
                </div>
              </Field>
              <div className="flex items-center gap-2">
                <Button
                  variant="primary"
                  size="sm"
                  onClick={submit}
                  disabled={submitting || polling}
                  data-evidence-id="studio.run.submit"
                >
                  {submitting ? "Submitting…" : polling ? "Running…" : "Run"}
                </Button>
                {(run !== null || runId !== null) && (
                  <Button size="sm" onClick={clear} data-evidence-id="studio.run.clear">
                    Clear
                  </Button>
                )}
              </div>
              {/* Same invocation as a shell command — the deployed graph IS an
                  API service; this is how apps outside the console call it. */}
              <details>
                <summary
                  className="cursor-pointer text-xs font-medium text-muted transition-colors hover:text-foreground"
                  data-evidence-id="studio.run.curl-toggle"
                >
                  Call this API with cURL
                </summary>
                <div className="mt-2">
                  <CurlBlock command={buildRunCurl(payload, threadId, servedCampaignId)} />
                </div>
              </details>
            </>
          )}

          {error && <ErrorBox message={error} />}

          {run && (
            <div className="space-y-2 text-sm">
              <div
                className="studio-run-summary"
                data-evidence-id="studio.run.summary"
              >
                <span>Run <strong className="font-mono" data-evidence-id="studio.run.current-id">{run.run_id}</strong></span>
                {run.thread_id && run.thread_id !== run.run_id && (
                  <span>Thread <strong className="font-mono">{run.thread_id}</strong></span>
                )}
                {run.current_step && <span>Step <strong className="font-mono">{run.current_step}</strong></span>}
              </div>
              {run.failure_state != null && (
                <div>
                  <details data-evidence-id="studio.run.failure-details">
                    <summary className="cursor-pointer text-xs font-medium text-red-700 dark:text-red-400">
                      Failure details{failedNode ? <> at <span className="font-mono">{failedNode}</span></> : null}
                    </summary>
                    <div className="mt-1.5"><Json value={run.failure_state} /></div>
                  </details>
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
    </StudioDialog>
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
          <li
            key={r.audit_id}
            className="space-y-1.5 py-2.5 first:pt-0 last:pb-0"
            data-evidence-scope={`audit-${r.audit_id}`}
          >
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
    <div className="w-96 max-w-[90vw] rounded-lg border border-border bg-surface shadow-md shadow-black/[0.08]">
      <header className="flex items-center justify-between gap-3 border-b border-border px-3 py-2">
        <span className="text-sm font-semibold text-foreground">
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
                className={`rounded-md bg-raised px-2 py-0.5 font-medium ${
                  issue.severity === "error"
                    ? "text-danger"
                    : "text-warning"
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
        className="w-full max-w-md rounded-lg border border-border bg-surface shadow-md shadow-black/[0.08]"
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
              <p className="rounded-lg border border-border bg-surface px-3 py-2 text-xs text-foreground">
                <strong className="text-success">Created</strong>{" — "}deployment <strong>{created.deployment_ref}</strong> v{created.version} created
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
  added: "bg-raised text-success",
  removed: "bg-raised text-danger",
  modified: "bg-raised text-warning",
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
        className="w-full max-w-2xl rounded-lg border border-border bg-surface shadow-md shadow-black/[0.08]"
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
                  <h3 className="mb-1.5 text-xs font-semibold text-muted">
                    {label} ({entries.length})
                  </h3>
                  <ul className="divide-y divide-border rounded-lg border border-border">
                    {entries.map((e, i) => (
                      <li
                        key={`${e.entity_id}-${i}`}
                        className="space-y-1.5 px-3 py-2 text-sm"
                        data-evidence-scope={`diff-${key}-${e.entity_id}-${e.change_type}`}
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span
                            className={`rounded-md px-2 py-0.5 text-xs font-medium ${CHANGE_TONE[e.change_type]}`}
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
