import { describe, expect, it } from "vitest";

import { DEFAULT_CONFIG, FIELD_SPECS, fieldSpecsForNodeType } from "./NodeInspector";
import { FALLBACK_NODE_TYPES } from "../lib/nodeTypes";
import { NODE_META } from "./nodeMeta";

describe("HTTP request node authoring contract", () => {
  it("keeps every configuration field visible for published API enum casing", () => {
    expect(fieldSpecsForNodeType("HTTP_REQUEST").map((field) => field.key)).toEqual(
      FIELD_SPECS.http_request.map((field) => field.key),
    );
  });

  it("exposes every bounded GET control and no method or credential control", () => {
    expect(FIELD_SPECS.http_request.map((field) => field.key)).toEqual([
      "url",
      "timeout_seconds",
      "max_retries",
      "retryable_status_codes",
      "max_response_bytes",
    ]);
    expect(DEFAULT_CONFIG.http_request).toEqual({
      url: "",
      timeout_seconds: 5,
      max_retries: 2,
      retryable_status_codes: [408, 429, 500, 502, 503, 504],
      max_response_bytes: 262144,
    });
  });

  it("is reachable from the offline palette and has operator guidance", () => {
    expect(FALLBACK_NODE_TYPES.some((type) => type.type === "http_request")).toBe(true);
    expect(NODE_META.http_request.help).toContain("GET");
  });
});
