import { describe, expect, it } from "vitest";

import { normalizeNodeType } from "./nodeTypes";

describe("normalizeNodeType", () => {
  it("maps API enum serialization to Studio registry keys", () => {
    expect(normalizeNodeType("HTTP_REQUEST")).toBe("http_request");
    expect(normalizeNodeType(" human_approval ")).toBe("human_approval");
  });
});
