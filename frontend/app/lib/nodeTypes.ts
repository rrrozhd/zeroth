import type { NodeType } from "@/app/lib/api";

/** API graph enums may be serialized in upper case; Studio uses lower-case keys. */
/** Node kinds the canvas can draw but an author may not place. */
export const IMPORTED_CATEGORY = "imported";

export function normalizeNodeType(value: string): string {
  return value.trim().toLowerCase();
}

// Offline mirror of the service's static node-type registry
// (src/zeroth/service/api/studio_api.py::_NODE_TYPES). When the API is
// unreachable the editor falls back to this list so the palette stays
// usable and loaded/inserted graphs keep their ports — without ports every
// edge referencing a handle id is silently dropped by React Flow and the
// canvas renders as disconnected cards.
//
// Keep in lockstep with the backend registry; it changes rarely and only
// additively (the canvas maps 1:1 to the executable graph model).

const IO_PORTS = [
  { id: "input-data", type: "data", direction: "input", label: "Input" },
  { id: "output-data", type: "data", direction: "output", label: "Output" },
] as const;

const AGENT_TOOLS_PORT = {
  id: "tools",
  type: "tool",
  direction: "output",
  label: "Tools",
} as const;

const TOOL_TARGET_PORT = {
  id: "tool-input",
  type: "tool",
  direction: "input",
  label: "Tool",
} as const;

export const FALLBACK_NODE_TYPES: NodeType[] = [
  {
    type: "entrypoint",
    label: "Entrypoint",
    category: "core",
    // Runs enter here — nothing upstream, so no input port.
    ports: [{ id: "output-data", type: "data", direction: "output", label: "Output" }]
  },
  { type: "agent", label: "Agent", category: "core", ports: [...IO_PORTS, AGENT_TOOLS_PORT] },
  { type: "code", label: "Code", category: "core", ports: [...IO_PORTS, TOOL_TARGET_PORT] },
  {
    type: "executable_unit",
    label: "Executable Unit",
    category: "core",
    ports: [...IO_PORTS, TOOL_TARGET_PORT]
  },
  { type: "human_approval", label: "Human Approval", category: "core", ports: [...IO_PORTS] },
  {
    type: "if",
    label: "If",
    category: "flow",
    ports: [
      { id: "input-data", type: "data", direction: "input", label: "Input" },
      { id: "true", type: "data", direction: "output", label: "True" },
      { id: "false", type: "data", direction: "output", label: "False" },
    ]
  },
  {
    type: "loop",
    label: "Loop",
    category: "flow",
    ports: [
      { id: "input-data", type: "data", direction: "input", label: "Input" },
      { id: "repeat", type: "data", direction: "output", label: "Repeat" },
      { id: "done", type: "data", direction: "output", label: "Done" },
      { id: "limit", type: "data", direction: "output", label: "Limit" },
    ]
  },
  { type: "retrieval", label: "Retrieval", category: "core", ports: [...IO_PORTS] },
  // Imported by `zeroth-core mcp-import`, never authored here, so it is drawable
  // but not creatable: it must have ports (an edge whose handle id is missing is
  // silently dropped) while staying out of the palette.
  {
    type: "mcp_tool",
    label: "MCP Tool",
    category: "imported",
    ports: [TOOL_TARGET_PORT],
  },
  { type: "http_request", label: "HTTP Request", category: "core", ports: [...IO_PORTS] },
  { type: "subgraph", label: "Subgraph", category: "core", ports: [...IO_PORTS] },
];
