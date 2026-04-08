# n8n Editor — Full Architecture & Layout Reference

Extracted from the n8n frontend codebase. Covers the complete spatial layout,
component hierarchy, canvas internals, node interactions, NDV (node settings),
executions view, and all supporting UI panels.

---

## 1. App Shell Layout

### 1.1 Root

```
App.vue
└── #n8n-app (height: 100vh; overflow: hidden)
    ├── <BaseLayout>
    │   └── <AppLayout>  ← CSS Grid container
    │       ├── #banners   (grid-area: banners)
    │       ├── #sidebar   (grid-area: sidebar)  ← MainSidebar
    │       ├── #header    (grid-area: header)   ← MainHeader
    │       ├── #content   (grid-area: content)  ← <RouterView> (NodeView, ExecutionsView, etc.)
    │       ├── #footer    (grid-area: footer)   ← LogsPanel (conditional)
    │       └── #aside     (grid-area: aside)    ← AppChatPanel
    ├── <AppModals>       (teleport target for NDV, dialogs)
    └── <AppCommandBar>
```

### 1.2 Grid CSS

```css
.app-grid {
  position: relative;
  display: grid;
  height: 100%;
  width: 100%;
  grid-template-areas:
    'banners banners banners'
    'sidebar header  aside'
    'sidebar content aside';
  grid-template-columns: auto 1fr auto;
  grid-template-rows: auto auto 1fr;
}

#content {
  display: flex;
  flex-direction: column;
  align-items: center;
  overflow: auto;
  grid-area: content;
}
```

### 1.3 Spatial Diagram

```
+-------------------------------------------------------------+
|                       #banners                               |
+---------+--------------------------------------+-------------+
| #sidebar|            #header                   |   #aside    |
| (42-    |  (MainHeader + TabBar)               | (ChatPanel) |
| 500px)  |                                      |             |
+---------+--------------------------------------+             |
|         |            #content                  |             |
|         |  (RouterView: NodeView /             |             |
|         |   ExecutionsView / etc.)             |             |
|         |                                      |             |
|         |  +--------------------------------+  |             |
|         |  | WorkflowCanvas or              |  |             |
|         |  | ExecutionsLandingPage           |  |             |
|         |  +--------------------------------+  |             |
|         |                                      |             |
+---------+--------------------------------------+-------------+
```

---

## 2. Sidebar

**Component:** `MainSidebar.vue` wrapping `<N8nResizeWrapper>`

```
<N8nResizeWrapper id="side-menu">
├── <MainSidebarHeader>       (logo + collapse toggle)
├── <N8nScrollArea>
│   └── <ProjectNavigation>   (workflow/credential tree)
├── <BottomMenu>              (Templates, Insights, Help, Settings)
├── <MainSidebarSourceControl>
└── <ResourceCenterTooltip>
```

```css
.sideMenu {
  position: relative;
  height: 100%;
  display: flex;
  flex-direction: column;
  border-right: var(--border);
  background-color: var(--menu--color--background, var(--color--background--light-2));
}

/* Collapsed */     width: 42px;
/* Expanded min */  width: 200px;
/* Expanded max */  width: 500px;   /* resizable, grid-size: 8px snap */
```

---

## 3. Header & Tab Bar

**Component:** `MainHeader.vue` → `TabBar.vue`

```
<div class="main-header">
├── <div class="top-menu">
│   └── <WorkflowDetails />    (workflow name, save status, share)
└── <TabBar />                 (centered, hangs below header bottom edge)
    └── <N8nRadioButtons>
        ├── RadioButton "Editor"     (value: 'workflow')
        ├── RadioButton "Executions" (value: 'executions')
        └── RadioButton "Tests"      (value: 'evaluation')
```

```css
.main-header {
  min-height: var(--navbar--height);
  background-color: var(--color--background--light-3);
  width: 100%;
  border-bottom: var(--border);
  position: relative;
  display: flex;
}

/* TabBar floats off the bottom of the header */
.tab-bar-container {
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%) translateY(50%);
  min-height: 30px;
  display: flex;
  padding: var(--spacing--5xs);           /* 2px */
  background-color: var(--color--foreground);
  border-radius: var(--radius);           /* 4px */
  z-index: 100;
}

/* Active tab */
.button.active {
  background-color: var(--color--foreground--tint-2);
  color: var(--color--text--shade-1);
}

/* Each button */
.button {
  height: 26px;
  font-size: var(--font-size--2xs);       /* 12px */
  padding: 0 var(--spacing--xs);          /* 12px */
  border-radius: var(--radius);
  transition: background-color 0.2s ease;
  cursor: pointer;
}
```

Tab selection calls `router.push()` — no `<router-link>`. Ctrl/Cmd-click opens in new tab.

---

## 4. Canvas Structure

### 4.1 Component Nesting

```
NodeView.vue
└── div.wrapper (display: flex; width: 100%)
    └── WorkflowCanvas.vue
        └── div.wrapper (display: flex; position: relative; width: 100%; height: 100%; overflow: hidden)
            └── div#canvas.canvas (width: 100%; height: 100%; background-color: var(--canvas--color--background))
                └── Canvas.vue
                    └── <VueFlow>   ← the actual @vue-flow/core root
                        ├── #node-canvas-node   → <CanvasNode> (per node)
                        ├── #edge-canvas-edge   → <CanvasEdge> (per edge)
                        ├── #connection-line     → <CanvasConnectionLine>
                        ├── <CanvasArrowHeadMarker>
                        ├── <CanvasBackground>   (dot grid, GRID_SIZE=8px)
                        ├── <MiniMap>            (Transition, 120x200, bottom-left)
                        ├── <CanvasControlButtons> (zoom/tidy controls, bottom-left)
                        └── <ContextMenu>
```

### 4.2 Canvas CSS

```css
/* WorkflowCanvas wrapper */
.wrapper {
  display: flex;
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
}

.canvas {
  width: 100%;
  height: 100%;
  position: relative;
  display: block;
  background-color: var(--canvas--color--background);
}

/* Canvas.vue (VueFlow host) */
.canvas {
  width: 100%;
  height: 100%;
  opacity: 0;
  transition: opacity 300ms ease;
}
.canvas.ready { opacity: 1; }
.canvas.isExperimentalNdvActive {
  --canvas-zoom-compensation-factor: 0.5;
}
```

### 4.3 VueFlow Config

```html
<VueFlow
  :apply-changes="false"
  :connection-radius="60"
  pan-on-scroll
  snap-to-grid
  :snap-grid="[GRID_SIZE, GRID_SIZE]"     /* [8, 8] */
  :min-zoom="0"
  :max-zoom="4"                            /* or experimentalNdvStore.maxCanvasZoom */
  :disable-keyboard-a11y="true"
  :delete-key-code="null"
/>
```

### 4.4 VueFlow Global Overrides (`_vueflow.scss`)

```css
/* Nodes */
.vue-flow__node, .vue-flow__node.draggable { cursor: pointer; }
.vue-flow__node.dragging { cursor: grabbing; }
.vue-flow__node:has(.sticky--active) { z-index: 1 !important; }
.vue-flow__node:has(.canvas-handle-plus):hover { z-index: 2 !important; }

/* Handles */
.vue-flow__handle:not(.connectionindicator) .plus {
  display: none;
  position: absolute;
}

/* Selection rect */
.vue-flow__nodesselection-rect {
  box-sizing: content-box;
  margin-top: calc(-1 * var(--spacing--2xs));
  margin-left: calc(-1 * var(--spacing--2xs));
  padding: var(--spacing--2xs);
}

/* Edges */
.vue-flow__edges:has(.bring-to-front),
.vue-flow__edge-label.selected { z-index: 1 !important; }

/* Pane */
.vue-flow__pane { cursor: grab; }
.vue-flow__pane.selection { cursor: default; }
.vue-flow__pane.dragging { cursor: grabbing; }

/* Controls */
.vue-flow__controls {
  margin: var(--spacing--sm);
  display: flex;
  gap: var(--spacing--xs);
  box-shadow: none;
}

/* Minimap */
.vue-flow__minimap {
  height: 120px;
  overflow: hidden;
  margin-bottom: calc(48px + 2 * var(--spacing--xs));
  border: var(--border);
  border-radius: var(--radius);
  background: var(--color--background--light-2);
}
.minimap-node-default { fill: var(--color--foreground--shade-1); }

/* Resizer (sticky notes) */
.vue-flow__resize-control.line { border-color: transparent; z-index: 1; }
.vue-flow__resize-control.handle {
  background-color: transparent;
  width: var(--spacing--sm);
  height: var(--spacing--sm);
  border: 0;
  border-radius: 0;
  z-index: 1;
}
```

### 4.5 Spotlight Mode

When the setup panel highlights specific nodes:

```css
.spotlightActive {
  :deep(.vue-flow__edges) { opacity: 0.2; transition: opacity 0.5s ease; }
  :deep(.vue-flow__node)  { opacity: 0.4; transition: opacity 0.5s ease; }
  :deep(.vue-flow__node:has(.highlighted)) { opacity: 1; }
}
```

### 4.6 Canvas Background

```html
<Background pattern-color="var(--canvas--dot--color)" :gap="GRID_SIZE" />
```

Read-only mode adds a striped SVG pattern overlay.

---

## 5. Canvas Tokens (Light / Dark)

```css
/* Light theme */
--canvas--color--background:          var(--color--neutral-125);
--canvas--dot--color:                 var(--color--neutral-500);
--canvas--color--selected:            var(--color--neutral-150);
--canvas--color--selected-transparent: hsla(220, 47%, 30%, 0.1);
--canvas--label--color:               var(--color--neutral-600);
--canvas--label--color--background:   oklch(from var(--canvas--color--background) l c h / 0.85);

/* Dark theme */
--canvas--color--background:          var(--color--neutral-950);
--canvas--dot--color:                 var(--color--neutral-700);
--canvas--color--selected:            var(--color--white-alpha-400);
--canvas--color--selected-transparent: var(--canvas--color--selected);
--canvas--label--color:               var(--color--neutral-500);
--canvas--label--color--background:   oklch(from var(--canvas--color--background) l c h / 0.85);
```

---

## 6. Node Elements

### 6.1 CanvasNode (Wrapper)

```
<div class="canvasNode" data-node-name data-node-type>
├── <CanvasHandleRenderer mode="outputs"> (per output port)
├── <div class="canvasNodeToolbarItems">     ← opacity: 0 → 1 on hover/select
│   └── <CanvasNodeToolbar>
├── <CanvasNodeRenderer>                     ← dispatches to render type
│   ├── CanvasNodeDefault
│   ├── CanvasNodeStickyNote
│   ├── CanvasNodeAddNodes
│   └── CanvasNodeChoicePrompt
├── <CanvasNodeTrigger>                      ← trigger bolt/chat button (left side)
└── <CanvasHandleRenderer mode="inputs">  (per input port)
```

Toolbar visibility CSS:
```css
.canvasNodeToolbarItems {
  opacity: 0;
  transition: opacity 0.1s ease-in-out;
}
.canvasNode:hover .canvasNodeToolbarItems,
.canvasNode:focus-within .canvasNodeToolbarItems { opacity: 1; }
```

Toolbar positioning:
```css
.canvasNodeToolbar {
  position: absolute;
  bottom: 100%;
  left: 50%;
  transform: translateX(-50%);
}
```

### 6.2 Default Node Rendering

```
<div class="node" style="--canvas-node--width; --canvas-node--height; --node--icon--size; ...">
├── <NodeIcon>                              (centered, 40px or 30px)
├── <CanvasNodeSettingsIcons>               (top-right grid of tiny indicators)
├── <CanvasNodeDisabledStrikeThrough>       (diagonal line overlay)
├── <div class="description">
│   ├── <span class="label">               (2-line clamp, 13px bold)
│   ├── <span class="disabledLabel">       (" (Disabled)")
│   └── <span class="subtitle">            (12px muted, 1-line ellipsis)
└── <CanvasNodeStatusIcons>                 (bottom-right: status dot, item count)
```

**Size computation** (`calculateNodeSize()`):
- Default: `[100, 100]`
- Grows based on input/output port counts and non-main connection counts
- Configuration nodes: circular, radius 36px
- GRID_SIZE (8px) used as the sizing unit

**State classes on `.node`:**
| Class | Visual |
|---|---|
| `.selected` | `box-shadow: 0 0 0 calc(6px * zoomComp) var(--canvas--color--selected-transparent)` |
| `.success` | 2px success-color border |
| `.warning` | 2px warning-color border |
| `.error` | danger-color border |
| `.pinned` | 2px secondary-color border |
| `.disabled` | greyed border + reduced opacity |
| `.running` | conic-gradient `::after` rotating at 1.5s |
| `.waiting` | conic-gradient `::after` rotating at 4.5s |
| `.configuration` | full circular `border-radius: calc(height / 2)` |
| `.trigger` | rounded top-left/bottom-left only |
| `.configurable` | flex-row layout, label shifts right of icon |

### 6.3 Sticky Note Rendering

```
<div class="sticky">
├── <NodeResizer min-height="80" min-width="150">
└── <N8nSticky>
    └── editable content area
```

```css
.sticky.selected {
  box-shadow: 0 0 0 4px var(--canvas--color--selected);
}
```

Z-index: base -100; smaller notes get higher z-index via overlap detection.

### 6.4 Node Toolbar

```
<div class="canvasNodeToolbar">
└── <div class="canvasNodeToolbarItems">    (flex, dark bg, rounded, padding)
    ├── Execute button (flask icon)
    ├── Disable button (power icon)
    ├── Delete button (trash icon)
    ├── Focus button (target icon, AI nodes)
    ├── Sticky color selector (conditional)
    ├── Add to AI button (conditional)
    └── Overflow menu (ellipsis → context menu)
```

```css
.canvasNodeToolbarItems {
  display: flex;
  align-items: center;
  background: dark;
  border-radius: rounded;
  pointer-events: auto;
  zoom: var(--canvas-zoom-compensation-factor, 1);
}
```

### 6.5 Node Status Icons

Positioned absolute bottom-right on the node:
- Disabled indicator (slash icon, foreground color)
- Execution error (warning icon, danger)
- Validation error (warning icon, warning)
- Pinned data (pin icon, secondary)
- Dirty state (dot, warning)
- Success (check icon, success)
- Run data count (badge showing iteration count)
- Optional spinner with scrim overlay during execution

---

## 7. Handle Elements

### 7.1 Handle Renderer

Each port renders a `<Handle>` from @vue-flow/core with a child render-type component.

```css
.handle {
  display: flex;
  align-items: center;
  justify-content: center;
  --handle--indicator--width: calc(16px * var(--canvas-zoom-compensation-factor, 1));
  --handle--indicator--height: calc(16px * var(--canvas-zoom-compensation-factor, 1));
}
```

Handle ID format: `"<mode>/<type>/<index>"` — e.g. `"outputs/main/0"`

### 7.2 Render Types

| Type | Shape | Position | Extra |
|---|---|---|---|
| MainInput | Dot | Left of node | Label on left, not draggable-from |
| MainOutput | Dot + Plus | Right of node | Label on right, run data badge above |
| NonMainInput | Diamond + Plus | Bottom of node | Label above, AI/tool ports |
| NonMainOutput | Diamond | Top/bottom of node | Label above |

### 7.3 Handle Shapes

**Dot** (main connections):
```css
.handleDot {
  border: oklch-adjusted border;
  background: var(--color--neutral-white);   /* dark: neutral-850 */
  border-radius: 50%;
  transition: all 0.2s ease;
}
.handleDot:hover { transform: scale(1.5); }
/* Output dots get cursor: crosshair */
```

**Diamond** (non-main connections):
```css
.handleDiamond {
  transform: rotate(45deg) scale(0.8);
  border-radius: 2px;
}
.handleDiamond:hover { transform: rotate(45deg) scale(1.2); }
```

**Plus button** (add connection):
- SVG line from handle + rect with centered plus path
- Position: top, right, bottom, or left of handle
- Success type = green line, secondary type = dashed
- Hover: color change + scale

---

## 8. Edge Elements

### 8.1 Edge Structure

```
<g class="edge" style="--canvas-edge--color; --lightness vars">
├── <BaseEdge> (per segment, with path)
└── <EdgeLabelRenderer>
    └── <div class="edgeLabelWrapper">
        ├── Label text (item count)     ← visible when not hovered
        └── <CanvasEdgeToolbar>         ← visible on hover
            ├── Add button (+)
            └── Delete button (trash)
```

### 8.2 Edge CSS

```css
.edge {
  stroke-width: calc(2 * var(--canvas-zoom-compensation-factor, 1)) !important;
  stroke: light-dark(
    oklch(var(--canvas-edge--color--lightness--light) 0 0),
    oklch(var(--canvas-edge--color--lightness--dark) 0 0)
  );
}

.edgeLabelWrapper {
  transform: scale(var(--canvas-zoom-compensation-factor, 1))
             translate(0, var(--label-translate-y));
  color: var(--canvas--label--color);
  background-color: var(--canvas--label--color--background);
}
```

**Non-main edges:** `stroke-dasharray: "5,6"`, `stroke-width: 2`

### 8.3 Edge Path

- **Forward connection** (source left of target): single Bezier curve
- **Backward connection** (source right of target): 2-segment smooth-step
  - Segment 1: right + down to midpoint (padding-bottom: 130px, padding-x: 40px)
  - Segment 2: left + up to target (border-radius: 16px)

### 8.4 Zoom-Adjusted Lightness

Gamma correction (gamma = 2.2) keeps edges perceptually visible at low zoom:
```typescript
function calculateZoomAdjustedValue(zoom, base, max, minZoom = 0.2, gamma = 2.2) {
  if (zoom >= 1.0) return base;
  if (zoom <= minZoom) return max;
  const t = (1.0 - zoom) / (1.0 - minZoom);
  return base + Math.pow(t, gamma) * (max - base);
}
```

### 8.5 Connection Line (In-Progress)

```css
.edge { opacity: 0; transition: opacity 300ms ease; }
.edge.visible { opacity: 1; }    /* after 300ms delay */
```

### 8.6 Arrow Marker

SVG `<marker>` with polyline `points="-5,-4 0,0 -5,4 -5,-4"`, size 12.5 strokeWidth units, `orient="auto-start-reverse"`. Uses `stroke="context-stroke"` to inherit edge color.

---

## 9. Control Buttons

```
<Controls>  ← @vue-flow/controls (bottom-left by default)
├── Zoom to Fit    (maximize icon, shortcut: 1)
├── Zoom In        (zoom-in icon, shortcut: +)
├── Zoom Out       (zoom-out icon, shortcut: -)
├── Toggle Zoom    (crosshair, shortcut: Z)       ← if experimental NDV
├── Reset Zoom     (undo-2 icon, shortcut: 0)     ← if zoom != 1
├── Tidy Up        (custom TidyUpIcon, Shift+Alt+T) ← if !readOnly
├── Expand All     (maximize-2)                    ← if experimental NDV
└── Collapse All   (minimize-2)                    ← if experimental NDV
```

```css
.vue-flow__controls {
  margin: var(--spacing--sm);      /* 16px */
  display: flex;
  gap: var(--spacing--xs);         /* 12px */
  box-shadow: none;
}
```

---

## 10. Node Detail View (NDV) — Node Settings Panel

### 10.1 Overlay

Teleported to `#app-modals`. Z-index: **1800**.

```
<Teleport to="#app-modals">
├── <div class="backdrop">     (full-screen, semi-transparent)
└── <dialog open class="dialog">
    ├── <NDVFloatingNodes>      (floating node selection UI)
    └── <div class="container">
        ├── <NDVHeader>
        │   ├── Node icon + inline-editable name
        │   ├── Docs link
        │   └── Close (X)
        └── <main class="main">   (3-column flex)
            ├── Input panel   (absolute left,  min-width: 280px)
            ├── Main panel    (absolute center, ~430px, resizable)
            │   ├── <PanelDragButton> (top-center resize handle)
            │   └── <NodeSettings>    (parameter form)
            └── Output panel  (absolute right, min-width: 280px)
```

### 10.2 NDV CSS

```css
.backdrop {
  position: absolute;
  z-index: var(--ndv--z);   /* 1800 */
  inset: 0;
  background-color: var(--dialog--overlay--color--background--dark);
}

.dialog {
  position: absolute;
  z-index: var(--ndv--z);
  width: calc(100% - var(--spacing--2xl));    /* 100% - 48px */
  height: calc(100% - var(--spacing--2xl));
  top: var(--spacing--lg);                     /* 24px */
  left: var(--spacing--lg);
  border: none;
  background: none;
  padding: 0;
  display: flex;
}

.container {
  display: flex;
  flex-direction: column;
  flex-grow: 1;
  background: var(--border-color);
  border: var(--border);
  border-radius: var(--radius--lg);            /* 8px */
}

.main {
  width: 100%;
  flex-grow: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
  position: relative;
}

.column + .column { border-left: var(--border); }
.input, .output { min-width: 280px; }
```

### 10.3 Panel Dimensions

```typescript
MAIN_NODE_PANEL_WIDTH = 430;   // default center panel
SIDE_MARGIN           = 24;
SIDE_PANELS_MARGIN    = 80;
MIN_PANEL_WIDTH       = 310;
PANEL_WIDTH           = 350;
PANEL_WIDTH_LARGE     = 420;
// wide mode: center = 860px
```

---

## 11. Node Creator Panel

### 11.1 Structure

```
<div>
├── <aside class="nodeCreatorScrim">    (fixed, behind panel)
├── <N8nIconButton class="close">       (mobile only)
└── <SlideTransition>
    └── <div class="nodeCreator">       (fixed, right side)
        └── <NodesListPanel>
            ├── Search bar
            ├── Category sections
            ├── Node list items
            └── Community nodes
```

### 11.2 CSS

```css
.nodeCreator {
  --node-creator--width: 385px;
  position: fixed;
  top: $header-height;
  bottom: 0;
  right: 0;                             /* shifts right by ChatPanel width if open */
  z-index: var(--node-creator--z);      /* 1700 */
  width: var(--node-creator--width);
}

.nodeCreatorScrim {
  position: fixed;
  top: $header-height;
  right: 0;
  bottom: 0;
  left: $sidebar-width;                 /* 42px or 200px */
  opacity: 0;
  z-index: 1;
  background: var(--dialog--overlay--color--background);
  pointer-events: none;
  transition: opacity 200ms ease-in-out;
}
.nodeCreatorScrim.active { opacity: 0.7; }

/* Responsive: full width minus sidebar when viewport is narrow */
@media (max-width: 427px) {             /* 385 + 42 */
  .nodeCreator { --node-creator--width: calc(100vw - 42px); }
  .close { display: block; }
}
```

---

## 12. Executions View

### 12.1 Structure

Renders in `#content` grid area when the "Executions" tab is active.

```
<div class="workflow-executions-container">
├── Empty state (v-if executionCount === 0)
│   └── Heading, text, action buttons
└── <WorkflowExecutionsList>
    ├── Filter bar
    ├── Execution cards/rows
    └── Pagination
```

### 12.2 CSS

```css
.container {
  width: 100%;
  height: 100%;
  flex: 1;
  background-color: var(--color--background--light-2);
  display: flex;
  flex-direction: column;
  align-items: center;
}
```

---

## 13. Z-Index Hierarchy

```
DRAGGABLE / ACTIVE_STICKY       9999999
CODEMIRROR_TOOLTIP / FLOATING_UI 3000
NPS_SURVEY_MODAL                 3001
ASK_ASSISTANT_FLOATING_BUTTON    3000
TOASTS                           2100
MODALS                           2000
DIALOGS                          1950
COMMAND_BAR                      1900
NDV                              1800
ASK_ASSISTANT_CHAT               1750
NODE_CREATOR                     1700
APP_SIDEBAR                       999
TOP_BANNERS                       999
CANVAS_ADD_BUTTON                 101
CANVAS_SELECT_BOX / SELECT_BOX    100
APP_HEADER                         99
CONTEXT_MENU                       10
```

---

## 14. Interaction Model

### 14.1 Panning & Selection

| Mode | Trigger | Behavior |
|---|---|---|
| Node drag | Left-click drag on node | Emits `update:nodes:position` on stop |
| Pan | Space + drag, or middle-mouse | `isInPanningMode = true` |
| Select | Shift + drag on pane | Rectangle multi-select |

Mobile: pan defaults to all mouse buttons; selection requires Shift.

### 14.2 Canvas Events (emits from Canvas.vue)

| User Action | Emit |
|---|---|
| Click node | `click:node` |
| Double-click node | `update:node:activated` |
| Right-click node | `open:contextmenu` |
| Drag stop | `update:nodes:position` |
| Click pane | `click:pane` |
| Start connection | `create:connection:start` |
| Complete connection | `create:connection` |
| Cancel connection | `create:connection:cancelled` |
| Delete connection | `delete:connection` |
| Right-click edge | `click:connection:add` |
| Viewport change | `viewport:change` |
| Delete/Copy/Duplicate/Cut | `delete:nodes` / `copy:nodes` / `duplicate:nodes` / `cut:nodes` |

### 14.3 Keyboard Shortcuts

**Navigation:** Arrow keys (traverse connections), Shift+Arrow (extend selection), Ctrl+A (select all)

**Editing:** F2 / short-Space (rename), Delete/Backspace (delete), Ctrl+X/C/D (cut/copy/duplicate), D (disable), P (pin)

**Canvas:** N (add node), Shift+S (sticky), 0 (reset zoom), 1 (fit view), Shift+Plus/Minus (zoom), Shift+Alt+T (tidy up), Ctrl+Enter (run)

---

## 15. Composables

| Name | Purpose |
|---|---|
| `useCanvas()` | Inject canvas context (`CanvasKey`) |
| `useCanvasNode()` | Inject node context + derived state |
| `useCanvasNodeHandle()` | Inject handle context |
| `useCanvasMapping()` | Workflow model → VueFlow nodes/edges |
| `useCanvasLayout()` | Dagre auto-layout ("Tidy Up") |
| `useCanvasNodeHover()` | Closest-node hit detection (200ms throttle) |
| `useCanvasTraversal()` | Graph navigation: parents, children, siblings, up/downstream |
| `useZoomAdjustedValues()` | Gamma-corrected zoom-compensated visual values |
| `useViewportAutoAdjust()` | Keep viewport center stable on resize |

---

## 16. Zoom Compensation Pattern

All visual elements scale inversely with zoom via CSS custom property:

```css
--canvas-zoom-compensation-factor: calc(1 / zoom);
```

Applied to:
- Node selection shadow: `calc(6px * var(--canvas-zoom-compensation-factor, 1))`
- Edge stroke width: `calc(2 * var(--canvas-zoom-compensation-factor, 1))`
- Handle indicator size: `calc(16px * var(--canvas-zoom-compensation-factor, 1))`
- Toolbar: `zoom: var(--canvas-zoom-compensation-factor, 1)`
- Labels: `transform: scale(var(--canvas-zoom-compensation-factor, 1))`
- Plus buttons: `transform: ... scale(var(--canvas-zoom-compensation-factor, 1))`

---

## 17. Responsive Breakpoints

```scss
$breakpoint-2xs: 600px;
$breakpoint-xs:  768px;
$breakpoint-sm:  992px;
$breakpoint-md:  1200px;
$breakpoint-lg:  1920px;
```

- Minimap hidden on `xs-only`
- Controls wrap/scroll on `xs-only`
- Node creator goes full-width (minus sidebar) below `~427px`
- Tab bar stacks vertically below `430px`
- Sidebar: min 200px, max 500px, collapses to 42px icon-only mode

---

## 18. Performance Patterns

- **Throttled node mapping:** `throttledRef(mappedNodes, 200)` used during execution
- **Throttled execution data:** 300ms throttle on run data updates
- **Delayed hover states:** edges 600ms, connection line 300ms visibility delay
- **Node data cache:** VueFlow reactivity workaround — `computed` building `nodeDataById` map
- **Minimap:** auto-hides after 1000ms of inactivity, transition 0.3s opacity
- **Canvas ready:** opacity 0→1 with 300ms transition on initialization
