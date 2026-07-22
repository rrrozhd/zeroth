// The Guide screen — an in-console primer: the three ideas behind a Zeroth
// deployment (Graphs / Contracts / Governance), a reference for the node types
// you'll see on the canvas and in timelines, and a copy-paste API quickstart so
// an operator can go from reading to a real run without leaving the console.
//
// Built on the P0 primitives in the P1/P2 house style (inline styles + CSS-var
// tokens, dark-only). This screen is fully static — no data loading, no hooks —
// so it stays a server component; only CodeBlock (a client primitive, for its
// copy button) crosses into the client tree.
//
// Node-type colors come from the shared NODE_TYPE_COLOR map (the same `--nt-*`
// tokens the canvas, palette, and node editor draw), so the swatches here match
// what the rest of the console shows. The quickstart never hardcodes a real key —
// it exports a $ZEROTH_API_KEY placeholder and references it in the curl header.

import {
  Card,
  CodeBlock,
  MonoLabel,
  NODE_TYPE_COLOR,
  Pill,
  StatusDot,
  type Tone,
} from "@/app/components/primitives";

// --------------------------------------------------------------------------
// Content — plain data, rendered below. Kept verbatim-close to the platform
// README's Key Concepts and node-type descriptions.
// --------------------------------------------------------------------------

const CONCEPTS: { title: string; body: string }[] = [
  {
    title: "Graphs",
    body: "Your application is an explicit graph of nodes wired by edges — agents, executable units, retrieval, and approval gates. It can branch, run in parallel, and cycle; every step is inspectable rather than hidden inside a prompt chain.",
  },
  {
    title: "Contracts",
    body: "Each node's inputs and outputs are typed contracts, validated at the boundary between nodes. A type error surfaces at the edge where it happens — caught at the node boundary, not buried deep inside a run.",
  },
  {
    title: "Governance",
    body: "Approvals pause runs for human sign-off, every node execution is recorded to a tamper-evident audit chain, spend is capped per tenant and per run, and capability policy is enforced fail-closed.",
  },
];

// Keyed by the shared node-type color token (NODE_TYPE_COLOR). `exec` is the
// token the exec-unit node draws.
const NODE_TYPES: { type: string; name: string; desc: string }[] = [
  {
    type: "entrypoint",
    name: "entrypoint",
    desc: "Where a run starts — its contract is the workflow's public input shape, validated before anything executes.",
  },
  {
    type: "agent",
    name: "agent",
    desc: "An LLM reasoning step, with optional memory connectors and other units attached as callable tools.",
  },
  {
    type: "exec",
    name: "exec unit",
    desc: "Sandboxed deterministic work — Python, scripts, commands, or a whole project, run under the sandbox backend.",
  },
  {
    type: "approval",
    name: "approval",
    desc: "A human-in-the-loop pause; the run holds until someone approves or rejects it in Approvals.",
  },
  {
    type: "retrieval",
    name: "retrieval",
    desc: "Queries a memory connector and passes the top matches downstream — the grounding step in a RAG flow.",
  },
  {
    type: "subgraph",
    name: "subgraph",
    desc: "Invokes another published graph as a single step, keeping workflows small and composable.",
  },
];

// The quickstart never embeds a real credential: $ZEROTH_API_KEY is exported as
// a placeholder and only referenced by name in the X-API-Key header.
const QUICKSTART = `# Set your operator key — a placeholder; never commit a real one.
export ZEROTH_API_KEY="your-operator-key"

# Submit a run against the deployed graph. The payload shape is defined
# by your graph's entrypoint (input) contract.
curl -X POST "http://127.0.0.1:8000/v1/runs" \\
  -H "X-API-Key: $ZEROTH_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"input_payload": {"question": "What is Zeroth?"}}'`;

const DOCS_LINKS: { label: string; href: string; kind: string; tone: Tone }[] = [
  {
    label: "Getting Started tutorial",
    href: "https://rrrozhd.github.io/zeroth-core/tutorials/getting-started/",
    kind: "tutorial",
    tone: "accent",
  },
  {
    label: "Governance Walkthrough",
    href: "https://rrrozhd.github.io/zeroth-core/tutorials/governance-walkthrough/",
    kind: "tutorial",
    tone: "agent",
  },
  {
    label: "Full documentation",
    href: "https://rrrozhd.github.io/zeroth-core/",
    kind: "docs",
    tone: "info",
  },
  {
    label: "Source on GitHub",
    href: "https://github.com/rrrozhd/zeroth-core",
    kind: "repo",
    tone: "neutral",
  },
];

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

export default function GuidePage() {
  return (
    <div className="z-fade" style={{ maxWidth: 1160, margin: "0 auto", padding: "26px 28px" }}>
      <header style={{ marginBottom: 18 }}>
        <h1 style={{ fontSize: 20, fontWeight: 600, letterSpacing: "-0.01em" }}>Guide</h1>
        <p style={{ marginTop: 4, fontSize: 13, color: "var(--text-muted)" }}>
          The core ideas behind a Zeroth deployment, the node types you&rsquo;ll build with, and how
          to call the API.
        </p>
      </header>

      <NoteCallout>
        A Zeroth app is an explicit <strong>graph</strong> whose node boundaries are guarded by
        typed <strong>contracts</strong> and run under <strong>governance</strong> — approvals,
        audit, and cost caps. Author it in Studio, then drive it over the same API the console uses.
      </NoteCallout>

      {/* Concepts */}
      <div style={{ height: 24 }} />
      <MonoLabel style={{ display: "block", marginBottom: 10 }}>Concepts</MonoLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
        {CONCEPTS.map((c) => (
          <Card key={c.title}>
            <div
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 12.5,
                fontWeight: 600,
                letterSpacing: "0.02em",
                color: "var(--accent)",
              }}
            >
              {c.title}
            </div>
            <p
              style={{
                margin: "8px 0 0",
                fontSize: 12.5,
                lineHeight: 1.6,
                color: "var(--text-muted)",
              }}
            >
              {c.body}
            </p>
          </Card>
        ))}
      </div>

      {/* Node types */}
      <div style={{ height: 26 }} />
      <MonoLabel style={{ display: "block", marginBottom: 10 }}>Node types</MonoLabel>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
        {NODE_TYPES.map((n) => (
          <Card key={n.type} pad={14}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span
                aria-hidden
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: 2,
                  background: NODE_TYPE_COLOR[n.type],
                  flexShrink: 0,
                }}
              />
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 12.5,
                  fontWeight: 600,
                  color: "var(--text-primary)",
                }}
              >
                {n.name}
              </span>
            </div>
            <p
              style={{
                margin: "8px 0 0",
                fontSize: 12,
                lineHeight: 1.55,
                color: "var(--text-muted)",
              }}
            >
              {n.desc}
            </p>
          </Card>
        ))}
      </div>

      {/* API quickstart */}
      <div style={{ height: 26 }} />
      <MonoLabel style={{ display: "block", marginBottom: 10 }}>API quickstart</MonoLabel>
      <Card>
        <p style={{ margin: "0 0 12px", fontSize: 12.5, lineHeight: 1.6, color: "var(--text-muted)" }}>
          Anything the console does, your application can do — it&rsquo;s a client of the same API.
          Submit a run with the operator key you connected with; the response includes a{" "}
          <code style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
            run_id
          </code>{" "}
          you can poll at{" "}
          <code style={{ fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>
            GET /v1/runs/&#123;run_id&#125;
          </code>
          .
        </p>
        <CodeBlock label="Shell" code={QUICKSTART} />
      </Card>

      {/* Docs links */}
      <div style={{ height: 26 }} />
      <MonoLabel style={{ display: "block", marginBottom: 10 }}>Documentation</MonoLabel>
      <Card pad={0} style={{ overflow: "hidden" }}>
        {DOCS_LINKS.map((l, i) => (
          <a
            key={l.href}
            href={l.href}
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              padding: "12px 16px",
              borderTop: i === 0 ? "none" : "1px solid var(--hair)",
              textDecoration: "none",
              color: "inherit",
            }}
          >
            <Pill tone={l.tone} style={{ flexShrink: 0 }}>
              {l.kind}
            </Pill>
            <span
              style={{
                minWidth: 0,
                flex: 1,
                fontSize: 12.5,
                fontWeight: 500,
                color: "var(--text-secondary)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {l.label}
            </span>
            <span
              aria-hidden
              style={{
                flexShrink: 0,
                fontFamily: "var(--font-mono)",
                fontSize: 13,
                color: "var(--accent)",
              }}
            >
              ↗
            </span>
          </a>
        ))}
      </Card>
    </div>
  );
}

// --------------------------------------------------------------------------
// Local bits (mirrors the reconciliation screen's teal-tinted callout).
// --------------------------------------------------------------------------

function NoteCallout({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        background: "color-mix(in srgb, var(--accent) 7%, transparent)",
        border: "1px solid color-mix(in srgb, var(--accent) 24%, transparent)",
        borderRadius: 8,
        padding: "11px 13px",
      }}
    >
      <StatusDot tone="accent" />
      <p style={{ margin: 0, fontSize: 12.5, lineHeight: 1.55, color: "var(--text-secondary)" }}>
        {children}
      </p>
    </div>
  );
}
