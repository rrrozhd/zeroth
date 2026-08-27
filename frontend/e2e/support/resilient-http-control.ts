import { execFileSync } from "node:child_process";
import path from "node:path";

const ALLOWED = new Set([
  "GET /control/events",
  "POST /control/recover",
  "POST /control/reset",
]);

const SCRIPT = [
  "import json, sys, urllib.request",
  "request = urllib.request.Request(sys.argv[2], method=sys.argv[1])",
  "with urllib.request.urlopen(request, timeout=5) as response:",
  "    body = response.read()",
  "    if body:",
  "        sys.stdout.buffer.write(body)",
  "    else:",
  "        print(json.dumps({'status_code': response.status}))",
].join("\n");

export function buildScenarioControlCommand(method: string, controlPath: string) {
  if (!ALLOWED.has(`${method} ${controlPath}`)) {
    throw new Error("resilient HTTP scenario control is outside the fixed allowlist");
  }
  return {
    executable: "docker",
    arguments: [
      "compose",
      "-f",
      "compose.dev.yml",
      "-f",
      "compose.resilient-http-live.yml",
      "exec",
      "-T",
      "backend",
      "python",
      "-c",
      SCRIPT,
      method,
      `http://127.0.0.1:8787${controlPath}`,
    ],
  };
}

export function runScenarioControl(method: string, controlPath: string): unknown {
  const command = buildScenarioControlCommand(method, controlPath);
  const output = execFileSync(command.executable, command.arguments, {
    cwd: path.resolve(process.cwd(), ".."),
    encoding: "utf-8",
    timeout: 10_000,
    maxBuffer: 128 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  });
  return JSON.parse(output);
}
