import { describe, expect, it } from "vitest";

import { containsSecretShape } from "../../e2e/support/secret-shapes";

describe("evidence secret-shape detection", () => {
  it("does not misclassify service authorization audit identities", () => {
    expect(containsSecretShape("service.authorization:35d04d87e12d4a318b261d6d09850e1a")).toBe(false);
  });

  it.each([
    "Authorization: Basic opaque-credential-material",
    "request headers\nAuthorization: opaque-credential-material",
    '{"Authorization":"opaque-credential-material"}',
    "Bearer abcdefghijklmnop",
    "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
  ])("rejects credential-bearing evidence: %s", (value) => {
    expect(containsSecretShape(value)).toBe(true);
  });
});
