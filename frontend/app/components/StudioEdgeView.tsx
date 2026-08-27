"use client";

import {
  BaseEdge,
  getSmoothStepPath,
  type Edge,
  type EdgeProps,
} from "@xyflow/react";
import {
  loopReturnPath,
  type EdgePresentation,
} from "@/app/studio/edit/graphPresentation";

export type StudioEdgeViewData = {
  kind: "data" | "tool";
  enabled: boolean;
  presentation?: EdgePresentation;
};

type StudioCanvasEdge = Edge<StudioEdgeViewData>;

export function StudioEdgeView({
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  data,
}: EdgeProps<StudioCanvasEdge>) {
  const presentation = data?.presentation;
  const role = presentation?.role ?? "default";
  const loopRoute = role === "loop-return"
    ? loopReturnPath({ sourceX, sourceY, targetX, targetY })
    : null;
  const [ordinaryPath] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 16,
    offset: 28,
  });
  const path = loopRoute?.path ?? ordinaryPath;
  const roleStyle = role === "loop-return"
    ? { stroke: "var(--accent)", strokeWidth: 2.5 }
    : role === "loop-continue"
      ? { stroke: "var(--accent)", strokeWidth: 2 }
      : role === "loop-exit"
        ? { stroke: "var(--text-muted)", strokeWidth: 1.75, strokeDasharray: "6 4" }
        : {};

  return (
    <>
      <BaseEdge
        path={path}
        markerEnd={markerEnd}
        style={{ ...style, ...roleStyle, opacity: data?.enabled === false ? 0.4 : style?.opacity }}
        className={`studio-edge-path is-${role}`}
        interactionWidth={24}
      />
    </>
  );
}
