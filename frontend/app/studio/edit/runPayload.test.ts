import { describe, expect, it } from "vitest";

import { examplePayloadFromSchema } from "../../lib/runPayload";

describe("examplePayloadFromSchema", () => {
  it("builds required object and array fields from the deployed input contract", () => {
    expect(
      examplePayloadFromSchema({
        type: "object",
        required: ["items"],
        properties: {
          items: {
            type: "array",
            minItems: 1,
            items: {
              type: "object",
              required: ["index", "query"],
              properties: {
                index: { type: "integer", minimum: 0 },
                query: { type: "string", minLength: 1 },
              },
            },
          },
          optional_note: { type: "string" },
        },
      }),
    ).toEqual({ items: [{ index: 0, query: "example" }] });
  });

  it("prefers explicit defaults, examples, and enum values", () => {
    expect(
      examplePayloadFromSchema({
        type: "object",
        required: ["mode", "count", "name"],
        properties: {
          mode: { type: "string", enum: ["safe", "fast"] },
          count: { type: "integer", default: 3 },
          name: { type: "string", examples: ["research"] },
        },
      }),
    ).toEqual({ mode: "safe", count: 3, name: "research" });
  });

  it("uses const values and satisfies simple anchored string patterns", () => {
    const schema = {
        type: "object",
        required: ["ticket", "status"],
        properties: {
          ticket: { type: "string", pattern: "^synthetic-" },
          status: { type: "string", const: "remediated" },
        },
      };
    const payload = examplePayloadFromSchema(schema);

    expect(payload).toEqual({ ticket: "synthetic-example", status: "remediated" });
    expect(new RegExp(schema.properties.ticket.pattern).test((payload as { ticket: string }).ticket)).toBe(true);
  });

  it("respects exact patterns, string lengths, and numeric bounds", () => {
    expect(
      examplePayloadFromSchema({
        type: "object",
        required: ["exact", "padded", "count", "ratio"],
        properties: {
          exact: { type: "string", pattern: "^foo$" },
          padded: { type: "string", minLength: 10 },
          count: { type: "integer", exclusiveMinimum: 5, multipleOf: 2 },
          ratio: { type: "number", minimum: 5, multipleOf: 4 },
        },
      }),
    ).toEqual({ exact: "foo", padded: "examplexxx", count: 6, ratio: 8 });
  });
});
