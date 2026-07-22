import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { STUDIO_CASES } from "./source-parity-cases";

const editor = readFileSync(resolve(import.meta.dirname, "../studio/edit/page.tsx"), "utf8");

describe("Studio semantic parity", () => {
  it.each(Object.entries(STUDIO_CASES))("%s retains its production seam", (_id, markers) => {
    for (const marker of markers) expect(editor.toLowerCase()).toContain(marker.toLowerCase());
  });
});
