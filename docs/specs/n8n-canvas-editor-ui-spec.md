# n8n Canvas Editor — UI Specification

Extracted from `packages/frontend/editor-ui/src/features/workflows/canvas/`.
Covers rendering, movement, editing interactions only — no backend/API references.

---

## 1. Node Rendering

### 1.1 Render Types

Four render types dispatched by `CanvasNodeRenderType` enum:

```typescript
enum CanvasNodeRenderType {
  Default = 'default',
  StickyNote = 'n8n-nodes-base.stickyNote',
  AddNodes = 'n8n-nodes-internal.addNodes',
  ChoicePrompt = 'n8n-nodes-internal.choicePrompt'
}
```

### 1.2 Default Node

**Options shape:**
```typescript
type CanvasNodeDefaultRender = {
  type: CanvasNodeRenderType.Default;
  options: Partial<{
    configurable: boolean;   // AI child-config nodes — horizontal label on right
    configuration: boolean;  // Small circular config node
    trigger: boolean;        // Circular trigger — rounded left, square right corners
    inputs:  { labelSize: 'small' | 'medium' | 'large' };
    outputs: { labelSize: 'small' | 'medium' | 'large' };
    tooltip?: string;
    dirtiness?: CanvasNodeDirtinessType;
    icon?: NodeIconSource;
    placeholder?: boolean;
  }>;
};
```

**Layout layers (back → front):**
1. Animated border glow (running / waiting states)
2. Background fill
3. Border (opacity varies by zoom)
4. Node icon (40 px default, 30 px for configuration nodes)
5. Label (2-line max)
6. Disabled label suffix
7. Subtitle
8. Status icons (execution result, pinned data, validation errors)

**CSS variables driven by data:**
```css
--canvas-node--width          /* computed from port counts */
--canvas-node--height         /* computed from port counts */
--node--icon--size            /* 40px or 30px */
--canvas-node--border--opacity-light
--canvas-node--border--opacity-dark
```

**Visual state classes:**
| Class | Style |
|---|---|
| `.selected` | 6 px primary-color shadow |
| `.success` | 2 px success-color border |
| `.warning` | 2 px warning-color border |
| `.error` | danger-color border |
| `.pinned` | 2 px secondary-color border |
| `.disabled` | greyed border + reduced opacity |
| `.running` | conic-gradient animated border (1.5 s) |
| `.waiting` | conic-gradient animated border (4.5 s) |
| `.configuration` | circle via `border-radius: height/2` |
| `.trigger` | rounded-left / square-right corners |
| `.configurable` | flex layout, label on right |

**Size calculation** (`calculateNodeSize()`):
- Inputs: configuration flag, configurable flag, trigger flag
- main-input count, main-output count, non-main connection count
- Whether experimental NDV is active

### 1.3 Sticky Note

**Options shape:**
```typescript
type CanvasNodeStickyNoteRender = {
  type: CanvasNodeRenderType.StickyNote;
  options: Partial<{
    width: number;
    height: number;
    color: number | string;  // 1–7 preset palette or hex
    content: string;
  }>;
};
```

- Wraps `N8nSticky` from `@n8n/design-system`
- Resizable via `NodeResizer` (min 80 × 150 px)
- Local `editMode` state; emits `update`, `move`, `activate`, `deactivate`
- Z-index: base −100, incremented by overlap detection so smaller notes surface above larger ones

### 1.4 Node Data Shape

```typescript
interface CanvasNodeData {
  id: string;
  name: string;
  subtitle: string;
  type: string;
  typeVersion: number;
  disabled: boolean;
  inputs:  CanvasConnectionPort[];
  outputs: CanvasConnectionPort[];
  connections: {
    inputs:  INodeConnections;
    outputs: INodeConnections;
  };
  issues: {
    execution: string[];
    validation: string[];
    visible: boolean;
  };
  pinnedData: { count: number; visible: boolean };
  execution: {
    status?: ExecutionStatus;
    waiting?: string;
    running: boolean;
    waitingForNext?: boolean;
  };
  runData: {
    outputMap?: ExecutionOutputMap;
    iterations: number;
    visible: boolean;
  };
  render: CanvasNodeDefaultRender | CanvasNodeStickyNoteRender | ...;
}
```

---

## 2. Movement & Dragging

### 2.1 Dragging Modes

Built on **VueFlow** internals.

| Mode | Trigger | Behaviour |
|---|---|---|
| Node drag | Default left-click drag on node | Emits `update:nodes:position` on drag stop |
| Pan | Space + drag, or middle-mouse drag | `isInPanningMode = true`; adjusts key/button bindings |
| Rectangular select | Shift + drag on empty pane | Multi-selects nodes inside rectangle |

### 2.2 Coordinate System

- 2D unbounded plane, origin top-left (0, 0), +X right / +Y down
- Viewport transform: `{ x, y, zoom }` (pan offsets + scale)

**Screen → canvas projection:**
```typescript
const projected = project({
  x: clientX - canvasBounds.left,
  y: clientY - canvasBounds.top,
});
```

**Canvas → screen:** CSS transform matrix on the viewport element (VueFlow internal).

### 2.3 Position Persistence

Nodes carry `position: { x, y }` in the workflow model (`INodeUi`).

On drag stop:
```typescript
emit('update:nodes:position', [{ id, position }, ...]);
```

### 2.4 Auto-Layout (Tidy Up)

`useCanvasLayout()` uses **Dagre.js**:
1. Decompose into connected subgraphs
2. Per subgraph: separate AI config chains → lay out vertically
3. Horizontal Dagre pass on main subgraph
4. Stack subgraphs vertically
5. Adjust AI node positions to avoid collisions
6. Move sticky notes below covered nodes

Spacing constants (multiples of `GRID_SIZE = 8 px`):
```
NODE_X_SPACING     = 64px   (8 × 8)
NODE_Y_SPACING     = 48px   (6 × 8)
SUBGRAPH_SPACING   = 64px
AI_X_SPACING       = 24px   (3 × 8)
AI_Y_SPACING       = 64px
STICKY_BOTTOM_PAD  = 32px   (4 × 8)
```

---

## 3. Selection & Editing

### 3.1 Selection

| Trigger | Action |
|---|---|
| Click node | Select single node |
| Ctrl/Cmd + click | Toggle node in selection |
| Shift + drag pane | Rectangular multi-select |
| Ctrl/Cmd + A | Select all |
| Shift + Arrow | Extend selection along connections |
| `nodes:select` event bus | Programmatic select by id array |

State stored in VueFlow (`selectedNodes`); canvas tracks `selectedNodeIds` computed and `lastSelectedNode` ref.

### 3.2 Node Parameter Editing

**Standard (external NDV):**
- Double-click → `emit('update:node:activated', id)`
- Opens side panel; parameter updates come back as `emit('update:node:parameters', id, parameters)`

**Experimental (inline NDV):**
- Feature-flagged via `isExperimentalNdvActive`
- Replaces node content with `ExperimentalInPlaceNodeSettings` form
- Activates/deactivates based on zoom level

### 3.3 Rename

- Trigger: F2 or short Space on selected node
- Emits: `emit('update:node:name', id)`
- Inline UI handled by parent

### 3.4 Port Updates

After config changes that alter input/output count:
```typescript
emit('update:node:inputs', id);
// or
emit('update:node:outputs', id);
// followed by:
await nextTick();
vueFlow.updateNodeInternals([id]);
```

---

## 4. Connection Ports & Handles

### 4.1 Port Model

```typescript
type CanvasConnectionPort = {
  node?: string;
  type: NodeConnectionType;  // 'main' | 'ai_tool' | 'ai_memory' | …
  index: number;
  required?: boolean;
  maxConnections?: number;
  label?: string;
};
```

### 4.2 Handle Render Types

Four components under `components/elements/handles/render-types/`:

| Component | Mode | Notes |
|---|---|---|
| `CanvasHandleMainInput` | Input | Not connectable by default |
| `CanvasHandleMainOutput` | Output | Dot + plus button |
| `CanvasHandleNonMainInput` | Input | Plus button, AI/tool ports |
| `CanvasHandleNonMainOutput` | Output | Dot only |

**Handle id string format:** `"<mode>/<type>/<index>"` — e.g. `"outputs/main/0"`, `"inputs/ai_tool/1"`

**Handle position:** inputs left, outputs right; configuration nodes place handles on circle perimeter; offsets calculated with GRID_SIZE spacing.

**Connection validation:**
```typescript
isConnectableStart = !connectionsLimitReached && (mode === Output || type !== Main);
isConnectableEnd   = !connectionsLimitReached && (mode === Input  || type !== Main);
```

**Zoom compensation on handles:**
```css
--handle--indicator--width:  calc(16px * var(--canvas-zoom-compensation-factor, 1));
--handle--indicator--height: calc(16px * var(--canvas-zoom-compensation-factor, 1));
```

### 4.3 Connection Creation Flow

```
onConnectStart  → connectingHandle = handle; connectionCreated = false
  [300 ms delay] → show dashed preview line (CanvasConnectionLine.vue)
onConnect       → emit('create:connection', connection); connectionCreated = true
onConnectEnd    →
  if connected  → emit('create:connection:end', handle, event)
  else          → emit('create:connection:cancelled', handle, position, event)
```

---

## 5. Edges

### 5.1 Edge Data

```typescript
interface CanvasConnectionData {
  source: CanvasConnectionPort;
  target: CanvasConnectionPort;
  status?: 'success' | 'error' | 'pinned' | 'running';
  maxConnections?: number;
}
```

### 5.2 Path Rendering (`getEdgeRenderData.ts`)

- **Default:** Bezier curve via `getBezierPath()`
- **Backward connection** (source right of target): two-segment path
  - Segment 1: right + down to midpoint
  - Segment 2: left + up to target
  - Constants: `EDGE_PADDING_BOTTOM=130`, `EDGE_PADDING_X=40`, `EDGE_BORDER_RADIUS=16`
- **Non-main connections:** dashed `strokeDasharray: "5,6"`, `strokeWidth: 2`

### 5.3 Zoom-Adjusted Edge Colors

Gamma-corrected perceptual brightness as zoom decreases (γ = 2.2):
```typescript
function calculateZoomAdjustedValue(zoom, base, max, minZoom = 0.2, gamma = 2.2) {
  if (zoom >= 1.0) return base;
  if (zoom <= minZoom) return max;
  const t = (1.0 - zoom) / (1.0 - minZoom);
  return base + Math.pow(t, gamma) * (max - base);
}
```

Stroke width also zoom-compensated:
```css
stroke-width: calc(2 * var(--canvas-zoom-compensation-factor, 1));
```

### 5.4 Edge Interaction

- Hover: 600 ms delay before state activates (prevents flicker); brings edge `zIndex: 1`
- Hover toolbar: Delete button + "Add node" (+) at path midpoint
- Right-click: `emit('click:connection:add', connection)`
- Delete: `emit('delete:connection', connection)`

---

## 6. Viewport

### 6.1 Zoom API

```typescript
zoomIn();
zoomOut();
zoomTo(level);           // e.g. 1.0
fitView({ maxZoom: 1.0, padding: 0.2 });
fitBounds(rect);
```

Range: min 0.2 — no hard max (typical cap 4.0).

### 6.2 Pan API

- Middle-mouse drag (native)
- Space + left-drag (programmatic pan mode)
- Scroll wheel pans (does not zoom)

### 6.3 Auto-Adjust on Resize

`useViewportAutoAdjust()` — ResizeObserver on viewport element:
```typescript
setViewport({
  x: viewport.x + (newWidth - oldWidth) / 2,
  y: viewport.y + (newHeight - oldHeight) / 2,
  zoom: viewport.zoom,
});
```
Keeps the canvas center stable when sidebars open/close.

### 6.4 Background Grid

- VueFlow `<Background>` component
- Dot pattern, `GRID_SIZE = 8 px`
- Color: `--canvas--dot--color`
- Optional striped SVG pattern via `CanvasBackgroundStripedPattern.vue`

---

## 7. State Model

### 7.1 Canvas Injection (`CanvasKey`)

```typescript
interface CanvasInjectionData {
  initialized: Ref<boolean>;
  isExecuting: Ref<boolean | undefined>;
  connectingHandle: Ref<ConnectStartEvent | undefined>;
  viewport: Ref<ViewportTransform>;
  isExperimentalNdvActive: ComputedRef<boolean>;
  isPaneMoving: Ref<boolean>;
}
```

### 7.2 Node Injection (`CanvasNodeKey`)

```typescript
interface CanvasNodeInjectionData {
  id: Ref<string>;
  data: Ref<CanvasNodeData>;
  label: Ref<string>;
  selected: Ref<boolean>;
  readOnly: Ref<boolean>;
  eventBus: Ref<EventBus<CanvasNodeEventBusEvents>>;
}
```

### 7.3 Handle Injection (`CanvasNodeHandleKey`)

```typescript
interface CanvasNodeHandleInjectionData {
  label: Ref<string | undefined>;
  mode: Ref<CanvasConnectionMode>;   // 'inputs' | 'outputs'
  type: Ref<NodeConnectionType>;
  index: Ref<number>;
  isRequired: Ref<boolean | undefined>;
  isConnected: ComputedRef<boolean | undefined>;
  isConnecting: Ref<boolean | undefined>;
  isReadOnly: Ref<boolean | undefined>;
  maxConnections: Ref<number | undefined>;
  runData: Ref<ExecutionOutputMapData | undefined>;
}
```

### 7.4 Execution Output Map

```typescript
type ExecutionOutputMap = {
  [connectionType: string]: {
    [outputIndex: string]: {
      total: number;
      iterations: number;
      byTarget?: {
        [targetNodeId: string]: { total: number; iterations: number };
      };
    };
  };
};
```

---

## 8. Event Bus Architecture

Three levels of loose coupling:

**Node-level** (per-node `EventBus`):
```typescript
type CanvasNodeEventBusEvents = {
  'update:sticky:color': never;
  'update:node:activated': never;
  'update:node:class': { className: string; add?: boolean };
};
```

**Canvas-level** (global `EventBus`):
```typescript
type CanvasEventBusEvents = {
  fitView: never;
  'saved:workflow': { isFirstSave: boolean };
  'open:execution': IExecutionResponse;
  'nodes:select': { ids: string[]; panIntoView?: boolean };
  'nodes:selectAll': never;
  'nodes:action': { ids: string[]; action: string; payload?: unknown };
  tidyUp: { source: CanvasLayoutSource; nodeIdsFilter?: string[] };
  'create:sticky': never;
};
```

**Vue emits** — parent component communication for workflow mutations.

---

## 9. Canvas Emits Reference

| User Action | Emit | Payload |
|---|---|---|
| Click node | `click:node` | id, position |
| Double-click node | `update:node:activated` | id, event |
| Node context menu | `open:contextmenu` | nodeId, event, source |
| Drag stop | `update:nodes:position` | `{ id, position }[]` |
| Click empty pane | `click:pane` | position |
| Start connection drag | `create:connection:start` | ConnectStartEvent |
| Connection made | `create:connection` | Connection |
| Connection finished | `create:connection:end` | Connection, event |
| Connection cancelled | `create:connection:cancelled` | handle, position, event |
| Delete connection | `delete:connection` | Connection |
| Right-click edge | `click:connection:add` | Connection |
| Viewport change | `viewport:change` | ViewportTransform, Dimensions |
| Delete nodes | `delete:nodes` | ids[] |
| Copy nodes | `copy:nodes` | ids[] |
| Duplicate nodes | `duplicate:nodes` | ids[] |
| Rename node | `update:node:name` | id |
| Tidy up | `tidy-up` | CanvasLayoutEvent |
| Create node | `create:node` | source string |
| Create sticky | `create:sticky` | — |
| Run node | `run:node` | id |

---

## 10. Keyboard Shortcuts

### Navigation
| Key | Action |
|---|---|
| Arrow keys | Navigate connections |
| Shift + Arrow | Extend selection along connections |
| Ctrl/Cmd + A | Select all |

### Editing
| Key | Action |
|---|---|
| F2 / short Space | Rename selected node |
| Space (hold) + drag | Pan canvas |
| Delete / Backspace | Delete selected |
| Ctrl+X / C / D | Cut / Copy / Duplicate |
| D | Toggle disable |
| P | Toggle pin data |

### Canvas
| Key | Action |
|---|---|
| N | Add node |
| Shift+S | Create sticky note |
| 0 | Reset zoom to 100% |
| 1 | Fit view |
| Shift+Plus / Minus | Zoom in / out |
| Shift+Alt+T | Tidy up (auto-layout) |
| Ctrl+Enter | Run workflow |

---

## 11. Composables

| Composable | Purpose |
|---|---|
| `useCanvas()` | Inject canvas-level context |
| `useCanvasNode()` | Inject node context + derived state |
| `useCanvasNodeHandle()` | Inject handle context |
| `useCanvasMapping()` | Transform workflow model → VueFlow nodes/edges |
| `useCanvasLayout()` | Dagre auto-layout |
| `useCanvasNodeHover()` | Closest-node hit detection (200 ms throttle) |
| `useCanvasTraversal()` | Graph navigation: parents, children, siblings, ancestors, descendants |
| `useZoomAdjustedValues()` | Gamma-corrected zoom-compensated visual values |
| `useViewportAutoAdjust()` | Keep viewport centered on resize |

---

## 12. CSS Variables Reference

### Canvas
```css
--canvas--dot--color
--canvas--label--color
--canvas--label--color--background
--canvas--color--selected-transparent
--canvas-zoom-compensation-factor    /* = 1 / zoom */
```

### Node
```css
--canvas-node--width
--canvas-node--height
--canvas-node--border-width
--node--icon--size
--canvas-node--border--opacity-light
--canvas-node--border--opacity-dark
--trigger-node--radius: 36px
--canvas-node--status-icons--margin
```

### Handles
```css
--handle--indicator--width
--handle--indicator--height
--handle--border--lightness--light
--handle--border--lightness--dark
```

### Edges
```css
--canvas-edge--color
--canvas-edge--color--lightness--light
--canvas-edge--color--lightness--dark
```

---

## 13. Key Implementation Patterns

**Composition over inheritance** — all interactive elements built from composables + `provide/inject`.

**Zoom compensation** — every stroke-width, handle size, and border opacity is multiplied by `--canvas-zoom-compensation-factor` (= `1 / zoom`) so elements stay perceptually consistent across zoom levels.

**Reactive mapping pipeline** — `useCanvasMapping()` runs a single computed chain from raw workflow data to VueFlow-compatible nodes/edges. Execution data updates are throttled to 300 ms.

**Three-level event architecture** — handle-level injection → node-level EventBus → canvas-level EventBus → Vue emits to parent — each level handles only its concern.

**Dagre for auto-layout** — layout is opt-in ("Tidy Up"), not continuous. The graph is decomposed into subgraphs first; AI configuration chains get a separate vertical pass before the main horizontal Dagre layout.

**Read-only mode** — single `readOnly` prop disables drag, editing shortcuts, context menu write actions, and hides edge toolbar.
