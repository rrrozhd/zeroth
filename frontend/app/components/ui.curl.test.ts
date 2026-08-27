// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";

import { setConfig } from "@/app/lib/config";
import { buildRunCurl } from "./ui";

describe("buildRunCurl", () => {
  beforeEach(() => {
    window.localStorage.clear();
    setConfig("http://127.0.0.1:8122", "service-secret-that-must-not-enter-clipboard");
  });

  it("copies an executable environment-variable command without embedding the configured key", () => {
    const command = buildRunCurl(
      '{"records":[{"name":"Ada"}]}',
      "thread-1",
      "evaluation-studio-v1",
    );

    expect(command).toMatch(/^curl -fsS -X POST /);
    expect(command).toContain('X-API-Key: $ZEROTH_API_KEY');
    expect(command).toContain('"thread_id": "thread-1"');
    expect(command).toContain('"campaign_id": "evaluation-studio-v1"');
    expect(command).not.toContain("service-secret-that-must-not-enter-clipboard");
    expect(command).not.toContain("<run_id>");
    expect(command.match(/\$ZEROTH_API_KEY/g)).toHaveLength(1);
  });
});
