import { describe, expect, it } from "vitest";

import {
  describeGraphEdges,
  layoutGraphNodes,
  loopReturnPath,
  shouldAutoLayoutLoopGraph,
  loopRouteCondition,
  ifRouteCondition,
  connectionClosesCycle,
} from "./graphPresentation";

const edges = [
  { id: "start-inspect", source: "start", target: "inspect" },
  {
    id: "inspect-repair",
    source: "inspect",
    target: "repair",
    condition: {
      expression: "payload.needs_repair == True and payload.repair_pass < 3",
      allow_cycle_traversal: false,
    },
  },
  {
    id: "repair-inspect",
    source: "repair",
    target: "inspect",
    condition: {
      expression: "True",
      allow_cycle_traversal: true,
      metadata: { purpose: "recheck_after_repair" },
    },
  },
  {
    id: "inspect-finalize",
    source: "inspect",
    target: "finalize",
    condition: {
      expression: "payload.needs_repair != True or payload.repair_pass >= 3",
      allow_cycle_traversal: false,
    },
  },
];

describe("loop graph presentation", () => {
  it("classifies the return, continue, and exit connections", () => {
    const described = describeGraphEdges(edges, new Map([
      ["inspect", "Inspect quality"],
      ["repair", "Repair records"],
    ]));

    expect(described.get("repair-inspect")).toMatchObject({
      role: "loop-return",
    });
    expect(described.get("inspect-repair")).toEqual({ role: "loop-continue" });
    expect(described.get("inspect-finalize")).toEqual({ role: "loop-exit" });
  });

  it("ranks nodes without treating the loop-back edge as forward progress", () => {
    const positions = layoutGraphNodes(
      ["start", "inspect", "repair", "finalize"],
      edges,
    );

    expect(positions.get("start")?.x).toBe(0);
    expect(positions.get("inspect")?.x).toBe(320);
    expect(positions.get("repair")?.x).toBe(640);
    expect(positions.get("finalize")?.x).toBe(640);
    expect(positions.get("repair")?.y).not.toBe(positions.get("finalize")?.y);
  });

  it("routes a loop return above the nodes with a stable label anchor", () => {
    const route = loopReturnPath({ sourceX: 850, sourceY: 180, targetX: 320, targetY: 300 });

    expect(route.path).toContain("M 850 180");
    expect(route.labelY).toBeLessThan(180);
    expect(route.labelX).toBeGreaterThan(320);
    expect(route.labelX).toBeLessThan(850);
  });

  it("auto-arranges only loop graphs whose branches collapse onto one row", () => {
    expect(shouldAutoLayoutLoopGraph([
      { id: "start", x: -420, y: 120 },
      { id: "inspect", x: -120, y: 120 },
      { id: "repair", x: 180, y: 120 },
      { id: "finalize", x: 480, y: 120 },
    ], edges)).toBe(true);

    expect(shouldAutoLayoutLoopGraph([
      { id: "start", x: 0, y: 0 },
      { id: "inspect", x: 320, y: 0 },
      { id: "repair", x: 640, y: -80 },
      { id: "finalize", x: 640, y: 80 },
    ], edges)).toBe(false);
  });

  it("maps named Loop outputs to deterministic runtime route conditions", () => {
    expect(loopRouteCondition("quality-loop", "repeat")).toEqual({
      expression: "payload.zeroth_loop['quality-loop'].route == 'repeat'",
      branch_rule: "expression",
      allow_cycle_traversal: true,
      metadata: { loop_route: "repeat" },
    });
    expect(loopRouteCondition("quality-loop", "done")?.allow_cycle_traversal).toBe(false);
    expect(loopRouteCondition("quality-loop", "input-data")).toBeNull();
  });

  it("maps named If outputs to deterministic hidden route conditions", () => {
    expect(ifRouteCondition("quality-gate", "true")).toEqual({
      expression: "payload.zeroth_if['quality-gate'].route == 'true'",
      branch_rule: "expression",
      allow_cycle_traversal: false,
      metadata: { if_route: "true" },
    });
    expect(ifRouteCondition("quality-gate", "false")?.metadata).toEqual({
      if_route: "false",
    });
    expect(ifRouteCondition("quality-gate", "critical")?.expression).toBe(
      "payload.zeroth_if['quality-gate'].route == 'critical'",
    );
    expect(ifRouteCondition("quality-gate", "input-data")).toBeNull();
  });

  it("detects only a connection that closes an existing path as a loop return", () => {
    const forward = [
      { id: "loop-inspect", source: "quality-loop", target: "inspect" },
      { id: "inspect-repair", source: "inspect", target: "repair" },
    ];

    expect(connectionClosesCycle(forward, "repair", "quality-loop")).toBe(true);
    expect(connectionClosesCycle(forward, "start", "quality-loop")).toBe(false);
  });

  it("labels dedicated Loop routes without exposing their runtime expressions", () => {
    const dedicated = [
      {
        id: "repeat",
        source: "retry",
        target: "inspect",
        condition: {
          expression: "payload.zeroth_loop['retry'].route == 'repeat'",
          allow_cycle_traversal: true,
          metadata: { loop_route: "repeat" },
        },
      },
      {
        id: "done",
        source: "retry",
        target: "report",
        condition: {
          expression: "payload.zeroth_loop['retry'].route == 'done'",
          allow_cycle_traversal: false,
          metadata: { loop_route: "done" },
        },
      },
      {
        id: "limit",
        source: "retry",
        target: "escalate",
        condition: {
          expression: "payload.zeroth_loop['retry'].route == 'limit'",
          allow_cycle_traversal: false,
          metadata: { loop_route: "limit" },
        },
      },
      {
        id: "legacy-template-route",
        source: "retry",
        target: "legacy-report",
        condition: {
          expression: "payload.zeroth_loop['retry'].route == 'done'",
          allow_cycle_traversal: false,
          metadata: { purpose: "loop_route", route: "done" },
        },
      },
    ];

    const described = describeGraphEdges(dedicated);

    expect(described.get("repeat")).toEqual({ role: "loop-continue" });
    expect(described.get("done")).toEqual({ role: "loop-exit" });
    expect(described.get("limit")).toEqual({ role: "loop-exit" });
    expect(described.get("legacy-template-route")).toEqual({ role: "loop-exit" });
  });
});
