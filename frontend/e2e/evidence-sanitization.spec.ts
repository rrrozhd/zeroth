import { expect, test } from "@playwright/test";

import {
  sanitizeUrl,
  summarizeRequest,
  summarizeResponse,
} from "./support/sanitized-network";
import { extractEvidenceIdentities } from "./support/live-evaluation";

test("sanitized network evidence excludes query strings, headers, and raw bodies", () => {
  const request = summarizeRequest({
    method: "POST",
    url: "https://example.test/v1/chat?api_key=must-not-survive#fragment",
    resourceType: "fetch",
    postData: '{"prompt":"synthetic"}',
  });

  expect(request).toEqual({
    method: "POST",
    url: "https://example.test/v1/chat",
    resource_type: "fetch",
    body_bytes: 22,
    body_sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
  });
  expect(JSON.stringify(request)).not.toContain("must-not-survive");
  expect(JSON.stringify(request)).not.toContain("synthetic");
});

test("response summaries retain only method-independent transport metadata", () => {
  expect(sanitizeUrl("http://127.0.0.1:3000/runs/1?token=nope")).toBe(
    "http://127.0.0.1:3000/runs/1",
  );
  expect(
    summarizeResponse({
      url: "http://127.0.0.1:3000/api/runs?token=nope",
      status: 200,
      resourceType: "xhr",
    }),
  ).toEqual({ url: "http://127.0.0.1:3000/api/runs", status: 200, resource_type: "xhr" });
});

test("URL sanitization redacts secret-shaped path segments", () => {
  const secret = `sk-proj-${"A".repeat(28)}`;
  const sanitized = sanitizeUrl(`https://example.test/v1/secrets/${secret}/status`);

  expect(sanitized).toBe("https://example.test/v1/secrets/[redacted]/status");
  expect(sanitized).not.toContain(secret);
});

test("response identity extraction retains only correlation allowlist fields", () => {
  const extracted = extractEvidenceIdentities({
    run_id: "run-1",
    nested: { audit_id: "audit-1", cost_event_id: "cost-1", output: "must-not-survive" },
    authorization: "must-not-survive",
  });

  expect(extracted).toEqual({
    run_id: ["run-1"],
    audit_id: ["audit-1"],
    cost_event_id: ["cost-1"],
  });
  expect(JSON.stringify(extracted)).not.toContain("must-not-survive");
});
