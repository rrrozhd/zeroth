import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { POLLING_MUTATION_CASES } from "./source-parity-cases";

const root = resolve(import.meta.dirname, "../..");

describe("polling and mutation parity", () => {
  it.each(Object.entries(POLLING_MUTATION_CASES))("%s retains its behavior seam", (_id, contract) => {
    const source = readFileSync(resolve(root, contract.file), "utf8");
    for (const marker of contract.markers) expect(source).toContain(marker);
  });
});
