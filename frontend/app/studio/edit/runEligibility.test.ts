import { describe, expect, it } from "vitest";

import { canDeployWorkflow, canRunWorkflow, servedGraphId } from "./runEligibility";

describe("servedGraphId", () => {
  it("extracts the graph id from a graph version ref", () => {
    expect(servedGraphId("graph-a@3")).toBe("graph-a");
  });

  it("returns the whole ref when there is no version suffix", () => {
    expect(servedGraphId("graph-a")).toBe("graph-a");
  });
});

describe("canRunWorkflow", () => {
  it("allows the workflow whose id matches the served deployment", () => {
    expect(canRunWorkflow("graph-a", "graph-a@3")).toBe(true);
  });

  it("rejects a workflow that is not the served deployment", () => {
    expect(canRunWorkflow("graph-b", "graph-a@3")).toBe(false);
  });

  it("rejects when nothing is served", () => {
    expect(canRunWorkflow("graph-a", null)).toBe(false);
  });
});

describe("canDeployWorkflow", () => {
  it("allows deploying published graphs only", () => {
    expect(canDeployWorkflow("published")).toBe(true);
    expect(canDeployWorkflow("draft")).toBe(false);
    expect(canDeployWorkflow("archived")).toBe(false);
  });
});
