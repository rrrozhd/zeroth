export type PresentationEdge = {
  id: string;
  source: string;
  target: string;
  condition?: {
    expression?: string;
    allow_cycle_traversal?: boolean;
    metadata?: Record<string, unknown>;
  } | null;
};

export function loopRouteCondition(nodeId: string, handleId: string | null | undefined) {
  if (handleId !== "repeat" && handleId !== "done" && handleId !== "limit") return null;
  const safeNodeId = nodeId.replaceAll("\\", "\\\\").replaceAll("'", "\\'");
  return {
    expression: `payload.zeroth_loop['${safeNodeId}'].route == '${handleId}'`,
    branch_rule: "expression" as const,
    allow_cycle_traversal: handleId === "repeat",
    metadata: { loop_route: handleId },
  };
}

export function ifRouteCondition(nodeId: string, handleId: string | null | undefined) {
  if (!handleId || handleId === "input-data" || handleId === "tools" || handleId === "tool-input") return null;
  const safeNodeId = nodeId.replaceAll("\\", "\\\\").replaceAll("'", "\\'");
  const safeRoute = handleId.replaceAll("\\", "\\\\").replaceAll("'", "\\'");
  return {
    expression: `payload.zeroth_if['${safeNodeId}'].route == '${safeRoute}'`,
    branch_rule: "expression" as const,
    allow_cycle_traversal: false,
    metadata: { if_route: handleId },
  };
}

export function connectionClosesCycle(
  edges: Pick<PresentationEdge, "source" | "target">[],
  source: string,
  target: string,
): boolean {
  const pending = [target];
  const seen = new Set<string>();
  while (pending.length > 0) {
    const current = pending.shift()!;
    if (current === source) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const edge of edges) {
      if (edge.source === current && !seen.has(edge.target)) pending.push(edge.target);
    }
  }
  return false;
}

export type EdgePresentation = {
  role: "default" | "conditional" | "loop-continue" | "loop-return" | "loop-exit";
};

function dedicatedLoopRoute(edge: PresentationEdge): "repeat" | "done" | "limit" | null {
  const metadata = edge.condition?.metadata;
  const candidate = metadata?.loop_route ?? (
    metadata?.purpose === "loop_route" ? metadata.route : null
  );
  return candidate === "repeat" || candidate === "done" || candidate === "limit"
    ? candidate
    : null;
}

export function describeGraphEdges(
  edges: PresentationEdge[],
  _nodeLabels: ReadonlyMap<string, string> = new Map(),
): Map<string, EdgePresentation> {
  const result = new Map<string, EdgePresentation>();
  const loopReturns = edges.filter(
    (edge) =>
      edge.condition?.allow_cycle_traversal && dedicatedLoopRoute(edge) !== "repeat",
  );

  for (const edge of edges) {
    const condition = edge.condition?.expression;
    result.set(edge.id, {
      role: condition ? "conditional" : "default",
    });
  }

  for (const edge of edges) {
    const route = dedicatedLoopRoute(edge);
    if (route === null) continue;
    result.set(edge.id, {
      role: route === "repeat" ? "loop-continue" : "loop-exit",
    });
  }

  for (const loop of loopReturns) {
    result.set(loop.id, {
      role: "loop-return",
    });

    for (const branch of edges.filter((edge) => edge.source === loop.target && edge.id !== loop.id)) {
      if (dedicatedLoopRoute(branch) !== null) continue;
      const continuesLoop = branch.target === loop.source;
      result.set(branch.id, {
        role: continuesLoop ? "loop-continue" : "loop-exit",
      });
    }
  }

  return result;
}

export function layoutGraphNodes(
  nodeIds: string[],
  edges: PresentationEdge[],
): Map<string, { x: number; y: number }> {
  const forwardEdges = edges.filter((edge) => !edge.condition?.allow_cycle_traversal);
  const levels = new Map(nodeIds.map((id) => [id, 0]));

  for (let pass = 0; pass < nodeIds.length; pass += 1) {
    let changed = false;
    for (const edge of forwardEdges) {
      const next = (levels.get(edge.source) ?? 0) + 1;
      if (next > (levels.get(edge.target) ?? 0)) {
        levels.set(edge.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }

  const columns = new Map<number, string[]>();
  for (const id of nodeIds) {
    const level = levels.get(id) ?? 0;
    columns.set(level, [...(columns.get(level) ?? []), id]);
  }

  const positions = new Map<string, { x: number; y: number }>();
  for (const [level, ids] of columns) {
    ids.forEach((id, index) => {
      positions.set(id, {
        x: level * 320,
        y: (index - (ids.length - 1) / 2) * 160,
      });
    });
  }
  return positions;
}

export function loopReturnPath(positions: {
  sourceX: number;
  sourceY: number;
  targetX: number;
  targetY: number;
}): { path: string; labelX: number; labelY: number } {
  const { sourceX, sourceY, targetX, targetY } = positions;
  const labelX = (sourceX + targetX) / 2;
  const labelY = Math.min(sourceY, targetY) - 92;
  const sourceControlX = sourceX + 72;
  const targetControlX = targetX - 72;
  return {
    path: `M ${sourceX} ${sourceY} C ${sourceControlX} ${sourceY}, ${sourceControlX} ${labelY}, ${labelX} ${labelY} C ${targetControlX} ${labelY}, ${targetControlX} ${targetY}, ${targetX} ${targetY}`,
    labelX,
    labelY,
  };
}

export function shouldAutoLayoutLoopGraph(
  nodes: { id: string; x: number; y: number }[],
  edges: PresentationEdge[],
): boolean {
  const positions = new Map(nodes.map((node) => [node.id, node]));
  for (const loop of edges.filter((edge) => edge.condition?.allow_cycle_traversal)) {
    const branchTargets = edges
      .filter((edge) => edge.source === loop.target && !edge.condition?.allow_cycle_traversal)
      .map((edge) => positions.get(edge.target))
      .filter((node): node is { id: string; x: number; y: number } => Boolean(node));
    if (branchTargets.length < 2) continue;
    const ys = branchTargets.map((node) => node.y);
    if (Math.max(...ys) - Math.min(...ys) < 96) return true;
  }
  return false;
}
