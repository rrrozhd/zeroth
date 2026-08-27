import { describe, expect, it } from "vitest";

import { buildScenarioControlCommand } from "../../e2e/support/resilient-http-control";

describe("resilient HTTP scenario control", () => {
  it("builds one credential-free fixed docker exec command", () => {
    const command = buildScenarioControlCommand("POST", "/control/reset");

    expect(command.executable).toBe("docker");
    expect(command.arguments).toContain("backend");
    expect(command.arguments).toContain("http://127.0.0.1:8787/control/reset");
    expect(command.arguments.join(" ")).not.toMatch(/authorization|api.?key|secret|token/i);
  });

  it.each([
    ["DELETE", "/control/reset"],
    ["POST", "/scenario/circuit"],
    ["GET", "/control/../../secrets"],
  ])("rejects non-allowlisted control %s %s", (method, path) => {
    expect(() => buildScenarioControlCommand(method, path)).toThrow(/allowlist/);
  });
});
