// Client-side presentation metadata for the executable node types: a short
// human description, a longer help paragraph (shown in the node editor and
// the Guide page), and an icon. Keyed by the backend node_type discriminator.

export const NODE_META: Record<string, { blurb: string; help: string; icon: string }> = {
  agent: {
    blurb: "LLM reasoning step",
    help: "Calls an LLM with your instruction. The upstream node's output arrives as context, and the model's reply flows downstream.",
    icon: "M9.6 3.2l1.3 3.5 3.5 1.3-3.5 1.3L9.6 12.8 8.3 9.3 4.8 8l3.5-1.3z M17 13l.8 2.2 2.2.8-2.2.8L17 19l-.8-2.2-2.2-.8 2.2-.8z",
  },
  executable_unit: {
    blurb: "Run code or a tool",
    help: "Runs registered code identified by its manifest ref — deterministic work such as API calls, data transforms, or side effects.",
    icon: "M8 6l-4 6 4 6 M16 6l4 6-4 6",
  },
  human_approval: {
    blurb: "Pause for human sign-off",
    help: "Pauses the run until a person approves or rejects it in the Approvals page. An optional SLA timeout escalates gates left unresolved.",
    icon: "M9 12l2 2 4-4 M12 3a9 9 0 100 18 9 9 0 000-18z",
  },
  retrieval: {
    blurb: "Retrieve grounding (RAG)",
    help: "Queries a knowledge/memory connector and passes the top matches downstream — the grounding step in a RAG flow.",
    icon: "M11 17a6 6 0 100-12 6 6 0 000 12z M21 21l-4.3-4.3",
  },
  subgraph: {
    blurb: "Invoke another graph",
    help: "Runs another published graph as a single step, so workflows stay small and composable.",
    icon: "M3 7l9-4 9 4-9 4-9-4z M3 12l9 4 9-4 M3 17l9 4 9-4",
  },
};

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
