<script setup lang="ts">
import { ref } from 'vue';
const railVisible = ref(true);
type Folder = {
  name: string;
  active: boolean;
  workflows: string[];
};

type Node = {
  title: string;
  subtitle: string;
  x: string;
  y: string;
  state: string;
  kind: string;
};

const folders: Folder[] = [
  {
    name: "Risk Ops",
    active: true,
    workflows: ["Risk Review Flow", "Fraud Escalation", "Policy Gate"],
  },
  {
    name: "Support",
    active: false,
    workflows: ["Support Triage", "Claims Intake"],
  },
  {
    name: "Payments",
    active: false,
    workflows: ["Chargeback Review", "Refund Routing"],
  },
];

const nodes: Node[] = [
  { title: "Trigger",  subtitle: "workflow entry",    x: "60px",  y: "100px", state: "DRAFT",      kind: "TRIGGER" },
  { title: "Agent",    subtitle: "policy evaluation", x: "280px", y: "100px", state: "RUNNING",    kind: "AGENT" },
  { title: "Approval", subtitle: "human checkpoint",  x: "500px", y: "100px", state: "CONFIGURED", kind: "APPROVAL" },
  { title: "Action",   subtitle: "downstream call",   x: "720px", y: "100px", state: "READY",      kind: "ACTION" },
  { title: "Memory",   subtitle: "context recall",    x: "280px", y: "330px", state: "LINKED",     kind: "MEMORY" },
];

const activeWorkflow = "Risk Review Flow";
const inspectorOpen = false;
</script>

<template>
  <div class="studio-shell">
    <div class="atmosphere atmosphere-left" />
    <div class="atmosphere atmosphere-right" />
    <div class="grid-texture" />

    <header class="studio-header panel">
      <div class="header-block">
        <div class="title-stack">
          <h1>Risk Review Flow</h1>
          <p><span class="save-dot" />Saved • Draft v3</p>
        </div>
      </div>

      <nav class="mode-switch" aria-label="Studio mode">
        <a class="mode-link active" href="/">Editor</a>
        <a class="mode-link" href="/">Executions</a>
      </nav>

      <div class="header-actions">
        <a href="/">Env / Dev</a>
        <a href="/">Publish</a>
      </div>
    </header>

    <div class="studio-body">
      <aside class="workflow-rail panel">
        <div class="rail-top">
          <div class="eyebrow">Workflows</div>
          <button class="rail-toggle" @click="railVisible = !railVisible" :title="railVisible ? 'Hide' : 'Show'">
            <span class="rail-toggle-icon">{{ railVisible ? '−' : '+' }}</span>
            <span class="rail-toggle-label">{{ railVisible ? 'hide' : 'show' }}</span>
          </button>
        </div>
        <button class="new-project-btn">
          <svg class="new-project-icon" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M7 2V12M2 7H12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          <span>New Project</span>
        </button>

        <div class="folder-list" v-show="railVisible">
          <section
            v-for="folder in folders"
            :key="folder.name"
            class="folder-block"
            :class="{ active: folder.active }"
          >
            <div class="folder-header">
              <div class="folder-figure" aria-hidden="true">
                <span class="folder-tab" />
                <span class="folder-body" />
              </div>
              <div class="folder-copy">
                <span class="folder-name">{{ folder.name }}</span>
              </div>
              <div class="folder-actions">
                <a class="folder-action" href="/" title="New workflow">+</a>
                <a class="folder-action" href="/" title="More actions">···</a>
              </div>
            </div>

            <div class="workflow-list">
              <div
                v-for="workflow in folder.workflows"
                :key="workflow"
                class="workflow-row"
                :class="{ active: workflow === activeWorkflow }"
              >
                <a class="workflow-name" href="/">{{ workflow }}</a>
                <div class="workflow-actions">
                  <a class="workflow-action" href="/" title="More actions">···</a>
                </div>
              </div>
            </div>
          </section>
        </div>


      </aside>

      <main class="editor-main">
        <section class="canvas-surface panel">
          <div class="canvas-dots" />

          <!-- Edges (SVG, pixel-positioned to match node handles) -->
          <!-- Node positions: Trigger@60, Agent@280, Approval@500, Action@720, Memory@280 -->
          <!-- Node width=160px, handle output at x+165, handle input at x-5 -->
          <!-- Top row y=100, node height~124, center y=162 -->
          <!-- Memory y=330, center y=392 -->
          <svg class="canvas-edges" xmlns="http://www.w3.org/2000/svg">
            <!-- Trigger output → Agent input -->
            <path class="canvas-edge" d="M 225 162 C 248 162 252 162 275 162" />
            <!-- Agent output → Approval input -->
            <path class="canvas-edge" d="M 445 162 C 468 162 472 162 495 162" />
            <!-- Approval output → Action input -->
            <path class="canvas-edge" d="M 665 162 C 688 162 692 162 715 162" />
            <!-- Agent bottom → Memory top (vertical) -->
            <path class="canvas-edge" d="M 360 224 C 360 260 360 295 360 330" />
            <!-- Memory output → Approval input (smooth arc) -->
            <path class="canvas-edge" d="M 445 392 C 475 372 485 182 495 162" />
          </svg>

          <!-- Nodes -->
          <article
            v-for="node in nodes"
            :key="node.title"
            class="graph-node"
            :class="[
              `node--${node.title.toLowerCase()}`,
              { 'node--running': node.state === 'RUNNING' },
              { 'node--selected': node.title === 'Approval' },
              { 'node--trigger': node.title === 'Trigger' },
            ]"
            :style="{ left: node.x, top: node.y }"
          >
            <!-- Hover toolbar (above node) -->
            <div class="node-toolbar">
              <button class="node-toolbar-btn" title="Run">
                <svg viewBox="0 0 12 12" fill="none"><path d="M3 2l7 4-7 4V2z" fill="currentColor"/></svg>
              </button>
              <button class="node-toolbar-btn" title="Disable">
                <svg viewBox="0 0 12 12" fill="none"><circle cx="6" cy="6" r="4.5" stroke="currentColor" stroke-width="1.2"/><line x1="6" y1="1.5" x2="6" y2="10.5" stroke="currentColor" stroke-width="1.2"/></svg>
              </button>
              <button class="node-toolbar-btn node-toolbar-btn--danger" title="Delete">
                <svg viewBox="0 0 12 12" fill="none"><path d="M2 3h8M5 3V2h2v1M4 3l.5 7h3L8 3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
              </button>
            </div>

            <!-- Input handle (left) -->
            <div class="node-handle node-handle--input" v-if="node.title !== 'Trigger'" />
            <!-- Output handle (right) -->
            <div class="node-handle node-handle--output" v-if="node.title !== 'Action'" />
            <!-- Bottom handle (Agent sends down to Memory) -->
            <div class="node-handle node-handle--bottom" v-if="node.title === 'Agent'" />
            <!-- Top handle (Memory receives from Agent) -->
            <div class="node-handle node-handle--top" v-if="node.title === 'Memory'" />

            <!-- Node icon (distinct per type) -->
            <div class="node-icon">
              <!-- Trigger: lightning bolt -->
              <svg v-if="node.title === 'Trigger'" viewBox="0 0 20 20" fill="none"><path d="M11 2L5 11h4l-1 7 6-9h-4l1-7z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
              <!-- Agent: sparkle/brain -->
              <svg v-else-if="node.title === 'Agent'" viewBox="0 0 20 20" fill="none"><path d="M10 2l1.5 4.5L16 8l-4.5 1.5L10 14l-1.5-4.5L4 8l4.5-1.5L10 2z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M15 13l.8 2.2L18 16l-2.2.8L15 19l-.8-2.2L12 16l2.2-.8L15 13z" stroke="currentColor" stroke-width="1" stroke-linejoin="round" opacity="0.6"/></svg>
              <!-- Approval: shield-check -->
              <svg v-else-if="node.title === 'Approval'" viewBox="0 0 20 20" fill="none"><path d="M10 2l6 3v5c0 3.5-2.5 6-6 7.5C6.5 16 4 13.5 4 10V5l6-3z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M7 10l2 2 4-4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
              <!-- Action: outgoing zap -->
              <svg v-else-if="node.title === 'Action'" viewBox="0 0 20 20" fill="none"><path d="M4 10h8M12 10l-3-3M12 10l-3 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><circle cx="15" cy="10" r="1.5" fill="currentColor"/></svg>
              <!-- Memory: database/stack -->
              <svg v-else viewBox="0 0 20 20" fill="none"><ellipse cx="10" cy="5.5" rx="6" ry="2.5" stroke="currentColor" stroke-width="1.3"/><path d="M4 5.5v9c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5v-9" stroke="currentColor" stroke-width="1.3"/><path d="M4 10c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5" stroke="currentColor" stroke-width="1.1"/></svg>
            </div>


            <div class="node-body">
              <div class="node-meta">
                <span class="node-kind">{{ node.kind }}</span>
                <span class="node-state">{{ node.state }}</span>
              </div>
              <h3>{{ node.title }}</h3>
              <p>{{ node.subtitle }}</p>
            </div>
          </article>

          <!-- Canvas controls (bottom-left) -->
          <div class="canvas-controls">
            <button class="canvas-ctrl" title="Fit view (1)">
              <svg viewBox="0 0 14 14" fill="none"><path d="M1 5V1h4M9 1h4v4M13 9v4H9M5 13H1V9" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
            </button>
            <button class="canvas-ctrl" title="Zoom in (+)">
              <svg viewBox="0 0 14 14" fill="none"><path d="M7 3v8M3 7h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            </button>
            <button class="canvas-ctrl" title="Zoom out (-)">
              <svg viewBox="0 0 14 14" fill="none"><path d="M3 7h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            </button>
            <button class="canvas-ctrl" title="Tidy up (Shift+Alt+T)">
              <svg viewBox="0 0 14 14" fill="none"><rect x="1" y="2" width="4" height="3" rx="0.8" stroke="currentColor" stroke-width="1.2"/><rect x="1" y="9" width="4" height="3" rx="0.8" stroke="currentColor" stroke-width="1.2"/><rect x="9" y="5.5" width="4" height="3" rx="0.8" stroke="currentColor" stroke-width="1.2"/><path d="M5 3.5h2.5V11H5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/><path d="M7.5 7h1.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
            </button>
            <div class="canvas-ctrl-divider" />
            <button class="canvas-ctrl" title="Add node (N)">
              <svg viewBox="0 0 14 14" fill="none"><path d="M7 2v10M2 7h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
            </button>
          </div>

          <!-- Minimap (bottom-left, above controls) -->
          <div class="canvas-minimap">
            <div class="minimap-node minimap-n1" />
            <div class="minimap-node minimap-n2" />
            <div class="minimap-node minimap-n3" />
            <div class="minimap-node minimap-n4" />
            <div class="minimap-node minimap-n5" />
            <div class="minimap-edge minimap-e1" />
            <div class="minimap-edge minimap-e2" />
            <div class="minimap-edge minimap-e3" />
            <div class="minimap-edge minimap-e4" />
            <div class="minimap-viewport" />
          </div>

        </section>
      </main>

      <aside v-if="inspectorOpen" class="inspector panel">
        <div class="eyebrow">Inspector</div>
      </aside>
    </div>
  </div>
</template>
