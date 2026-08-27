import { describe, expect, it } from "vitest";

import { DEFAULT_CONFIG, FIELD_SPECS } from "./NodeInspector";
import { NODE_META } from "./nodeMeta";
import { FALLBACK_NODE_TYPES } from "@/app/lib/nodeTypes";

describe("Loop node authoring contract", () => {
  it("offers the bounded retry configuration in the inspector", () => {
    expect(FIELD_SPECS.loop.map((field) => field.key)).toEqual([
      "until",
      "max_retries",
    ]);
    expect(DEFAULT_CONFIG.loop).toEqual({ until: "", max_retries: 3 });
  });

  it("keeps the offline palette and visible node metadata in sync", () => {
    const loop = FALLBACK_NODE_TYPES.find((type) => type.type === "loop");

    expect(loop?.ports.map((port) => port.id)).toEqual([
      "input-data",
      "repeat",
      "done",
      "limit",
    ]);
    expect(NODE_META.loop.blurb).toContain("retry");
  });
});
