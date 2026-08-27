import { describe, expect, it } from "vitest";

import { findArtifactReferences } from "./artifact-references";

describe("findArtifactReferences", () => {
  it("discovers canonical artifact references from nested run outputs", () => {
    expect(
      findArtifactReferences({
        report: {
          artifact: {
            key: "run-1/node/report",
            content_type: "application/json",
            size: 42,
          },
        },
      }),
    ).toEqual([
      {
        key: "run-1/node/report",
        contentType: "application/json",
        size: 42,
      },
    ]);
  });

  it("deduplicates references and ignores unrelated key-shaped objects", () => {
    expect(
      findArtifactReferences({
        first: { artifact: { key: "run-1/node/report" } },
        second: [{ artifact: { key: "run-1/node/report" } }, { key: "not-an-artifact" }],
      }),
    ).toEqual([{ key: "run-1/node/report", contentType: null, size: null }]);
  });
});
