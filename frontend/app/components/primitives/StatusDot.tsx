// UI primitive: an 8px status indicator dot.
// Pure presentational — safe in both server and client component trees.

/** Tone -> CSS color variable. Shared by StatusDot, Pill, and other atoms. */
export const TONE: Record<string, string> = {
  accent: "var(--accent)",
  success: "var(--success)",
  warning: "var(--warning)",
  danger: "var(--danger)",
  muted: "var(--text-faint)",
  info: "var(--info)",
  agent: "var(--agent)",
  neutral: "var(--neutral)",
};

export type Tone = keyof typeof TONE;

export function StatusDot({
  tone = "muted",
  pulse = false,
}: {
  tone?: Tone | string;
  pulse?: boolean;
}) {
  return (
    <span
      className={pulse ? "z-pulse" : ""}
      aria-hidden
      style={{
        display: "inline-block",
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: TONE[tone as string] ?? tone,
      }}
    />
  );
}

/** Graph node-type -> its `--nt-*` color variable. */
export const NODE_TYPE_COLOR: Record<string, string> = {
  entrypoint: "var(--nt-entrypoint)",
  agent: "var(--nt-agent)",
  exec: "var(--nt-exec)",
  approval: "var(--nt-approval)",
  retrieval: "var(--nt-retrieval)",
  subgraph: "var(--nt-subgraph)",
};

/** Color var for a graph node type; falls back to the neutral (subgraph) color. */
export function nodeTypeColor(type: string): string {
  return NODE_TYPE_COLOR[type] ?? "var(--nt-subgraph)";
}
