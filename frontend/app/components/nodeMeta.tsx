// Client-side presentation metadata for the executable node types: a short
// human description, a longer help paragraph (shown in the node editor and
// the Guide page), an icon, and the node-type color. Keyed by the backend
// node_type discriminator. `color` maps each backend type onto the shared
// `--nt-*` design tokens (handoff node-type colors) so the canvas card, the
// palette, and the editor header all draw the same swatch. Multiple backend
// types collapse onto one token (code + executable_unit -> exec).
export const NODE_META: Record<string, { blurb: string; help: string; icon: string; color: string }> = {
  entrypoint: {
    blurb: "Where a run starts",
    help: "Every workflow starts here. The contract you pick is the workflow's public input shape — POST /v1/runs payloads are validated against it before anything executes. One per workflow, nothing flows into it.",
    icon: "M8 5v14l11-7-11-7z",
    color: "var(--nt-entrypoint)",
  },
  agent: {
    blurb: "LLM reasoning step",
    help: "Calls an LLM with your instruction. The upstream node's output arrives as context, and the model's reply flows downstream.",
    icon: "M9.6 3.2l1.3 3.5 3.5 1.3-3.5 1.3L9.6 12.8 8.3 9.3 4.8 8l3.5-1.3z M17 13l.8 2.2 2.2.8-2.2.8L17 19l-.8-2.2-2.2-.8 2.2-.8z",
    color: "var(--nt-agent)",
  },
  code: {
    blurb: "Write Python, run sandboxed",
    help: "Runs the Python you write right here. The upstream payload arrives as JSON on stdin; whatever you write to stdout as JSON flows downstream. Runs in the sandbox with the standard library only — the code is frozen and content-hashed when the workflow publishes.",
    icon: "M7 8l-4 4 4 4 M17 8l4 4-4 4 M14 4l-4 16",
    color: "var(--nt-exec)",
  },
  executable_unit: {
    blurb: "Run code or a tool",
    help: "Runs registered code identified by its manifest ref — deterministic work such as API calls, data transforms, or side effects.",
    icon: "M8 6l-4 6 4 6 M16 6l4 6-4 6",
    color: "var(--nt-exec)",
  },
  human_approval: {
    blurb: "Pause for human sign-off",
    help: "Pauses the run until a person approves or rejects it in the Approvals page. An optional SLA timeout escalates gates left unresolved.",
    icon: "M9 12l2 2 4-4 M12 3a9 9 0 100 18 9 9 0 000-18z",
    color: "var(--nt-approval)",
  },
  if: {
    blurb: "Two-way decision",
    help: "Evaluates one expression and sends the payload through the True or False output. The condition lives here, not on connection labels.",
    icon: "M4 12h5 M9 12l4-5 M9 12l4 5 M13 7h7 M13 17h7",
    color: "var(--nt-loop)",
  },
  loop: {
    blurb: "Bounded retry controller",
    help: "Runs its body once, then evaluates the done condition after each return. Repeat permits additional attempts up to Max retries; Done means the condition matched; Limit means the retry budget was exhausted.",
    icon: "M17 2l4 4-4 4 M21 6h-7a7 7 0 00-7 7 M7 22l-4-4 4-4 M3 18h7a7 7 0 007-7",
    color: "var(--nt-loop)",
  },
  retrieval: {
    blurb: "Retrieve grounding (RAG)",
    help: "Queries a knowledge/memory connector and passes the top matches downstream — the grounding step in a RAG flow.",
    icon: "M11 17a6 6 0 100-12 6 6 0 000 12z M21 21l-4.3-4.3",
    color: "var(--nt-retrieval)",
  },
  http_request: {
    blurb: "Resilient private GET",
    help: "Performs a bounded GET through Zeroth's resilient HTTP client with retries, timeout, circuit breaking, capability enforcement, sanitized signed audit facts, and measured zero provider cost. This first slice accepts only localhost or literal private-network addresses.",
    icon: "M10 13a5 5 0 007.1 0l2-2a5 5 0 00-7.1-7.1l-1.1 1.1 M14 11a5 5 0 00-7.1 0l-2 2A5 5 0 0012 20.1l1.1-1.1",
    color: "var(--nt-retrieval)",
  },
  mcp_tool: {
    blurb: "Call a pinned MCP tool",
    help: "Calls one tool on an MCP server an operator registered. The tool's schema was frozen when it was imported, and the runtime refuses to call it if the live server no longer matches. Unlike a code or executable-unit step, an MCP call is delivered at-least-once: it carries no operation receipt, so a retry can run it twice with nothing to suppress the duplicate. Imported with `zeroth-core mcp-import`, not authored here.",
    icon: "M4 7h16 M4 12h16 M4 17h10 M18 15l3 3-3 3",
    color: "var(--nt-mcp)",
  },
  subgraph: {
    blurb: "Invoke another graph",
    help: "Runs another published graph as a single step, so workflows stay small and composable.",
    icon: "M3 7l9-4 9 4-9 4-9-4z M3 12l9 4 9-4 M3 17l9 4 9-4",
    color: "var(--nt-subgraph)",
  },
};

/** Node-type color token for a backend node type; falls back to the neutral
 *  subgraph swatch for unknown types. */
export function nodeMetaColor(type: string): string {
  return NODE_META[type]?.color ?? "var(--nt-subgraph)";
}

/** Render a node type's icon as inline SVG (multi-subpath aware). */
export function NodeGlyph({ type, className = "h-4 w-4" }: { type: string; className?: string }) {
  const icon = NODE_META[type]?.icon;
  if (!icon) return null;
  const paths = icon.split(" M").map((seg, i) => (i === 0 ? seg : `M${seg}`));
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      {paths.map((d, i) => (
        <path key={i} d={d} />
      ))}
    </svg>
  );
}
