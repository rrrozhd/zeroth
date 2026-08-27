// @vitest-environment jsdom

import type { AnchorHTMLAttributes } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listAudits: vi.fn(),
  getAuditReadiness: vi.fn(),
  deploymentRef: vi.fn(),
  verifyDeploymentAuditChain: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

vi.mock("@/app/lib/config", () => ({ isConfigured: () => true }));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import AuditPage from "./page";

const workflowRecord = {
  audit_id: "audit-workflow",
  attempt: 1,
  chain_sequence: 2,
  deployment_ref: "research@1",
  digest_version: 1,
  erased: false,
  graph_version_ref: "research@1",
  node_id: "researcher",
  node_version: 1,
  run_id: "run-workflow",
  started_at: "2026-08-23T12:00:00Z",
  status: "completed",
  tenant_id: "tenant-1",
};

const securityRecord = {
  ...workflowRecord,
  audit_id: "audit-security",
  chain_sequence: 1,
  node_id: "service.authorization",
  run_id: "request-security",
  status: "allowed",
};

let container: HTMLDivElement;
let root: Root;

function button(label: string): HTMLButtonElement | undefined {
  return Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
}

async function waitFor(assertion: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      failure = error;
      await act(async () => {
        await new Promise((resolve) => window.setTimeout(resolve, 0));
      });
    }
  }
  throw failure;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  api.listAudits.mockResolvedValue({ records: [workflowRecord, securityRecord] });
  api.getAuditReadiness.mockResolvedValue({
    state: "signed",
    deployment_mode: "local",
    message: "Audit signing is configured.",
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("audit record views", () => {
  it("opens on workflow records while keeping all and security records reachable", async () => {
    await act(async () => root.render(<AuditPage />));

    await waitFor(() => expect(button("All")).toBeTruthy());

    expect(button("Workflow")?.getAttribute("aria-pressed")).toBe("true");
    expect(container.textContent).toContain("researcher");
    expect(container.textContent).not.toContain("service.authorization");
    expect(container.textContent).toContain("1 of 2 records");
    expect(container.textContent).toContain(
      "Workflow execution and service security records across this tenant.",
    );
    expect(container.textContent).toContain("Verify chain checks the actively served deployment");
    expect(container.textContent).toContain(
      "Workflow includes execution evidence. Security includes service authentication and authorization decisions.",
    );
  });

  it("explains metadata-only capture without rendering secret-redaction markers", async () => {
    api.listAudits.mockResolvedValue({
      records: [{
        ...workflowRecord,
        audit_id: "audit-redacted",
        error: "***REDACTED***",
        status: "failed",
      }],
    });

    await act(async () => root.render(<AuditPage />));
    await waitFor(() => expect(button("All")).toBeTruthy());

    expect(container.textContent).not.toContain("***REDACTED***");
    expect(container.textContent).toContain(
      "Payload values are withheld; correlation IDs, digests, signatures, status, and timing remain reviewable.",
    );
  });

  it("orders tenant-wide records by newest activity and labels sequence as chain-local", async () => {
    api.listAudits.mockResolvedValue({
      records: [
        workflowRecord,
        {
          ...workflowRecord,
          audit_id: "audit-newer",
          chain_sequence: 2,
          node_id: "finalize",
          run_id: "run-newer",
          started_at: "2026-08-23T13:00:00Z",
        },
      ],
    });

    await act(async () => root.render(<AuditPage />));
    await waitFor(() => expect(button("Workflow")).toBeTruthy());

    const rows = Array.from(container.querySelectorAll('[role="row"]'));
    expect(rows[0]?.textContent).toContain("chain #");
    expect(rows[1]?.textContent).toContain("run-newer");
    expect(rows[2]?.textContent).toContain("run-workflow");
  });

  it("filters persisted service authorization records without hiding them from All", async () => {
    await act(async () => root.render(<AuditPage />));
    await waitFor(() => expect(button("Workflow")).toBeTruthy());

    expect(container.textContent).toContain("researcher");
    expect(container.textContent).not.toContain("service.authorization");
    expect(container.textContent).toContain("1 of 2 records");

    await act(async () => button("Security")?.click());
    expect(container.textContent).not.toContain("researcher");
    expect(container.textContent).toContain("service.authorization");
    expect(container.textContent).toContain("1 of 2 records");

    await act(async () => button("All")?.click());
    expect(container.textContent).toContain("researcher");
    expect(container.textContent).toContain("service.authorization");
  });

  it("surfaces the exact failed audit record when verification rejects a broken chain", async () => {
    api.deploymentRef.mockResolvedValue("research-deployment");
    api.verifyDeploymentAuditChain.mockResolvedValue({
      scope: "deployment:research-deployment",
      verified: false,
      record_count: 2,
      failed_audit_id: "audit-workflow",
      error: null,
      signature_verified: false,
      signing_key_id: "local-evaluation",
      unsigned_record_count: 0,
    });

    await act(async () => root.render(<AuditPage />));
    await waitFor(() => expect(button("Verify chain")).toBeTruthy());

    const verify = button("Verify chain")!;
    expect(verify.dataset.evidenceId).toBe("audit.verify-chain");
    await act(async () => verify.click());

    await waitFor(() => {
      expect(container.textContent).toContain("chain broken at audit-workflow");
    });
    const status = container.querySelector('[data-evidence-id="audit.verify-chain.result"]');
    expect(status?.getAttribute("data-tone")).toBe("danger");
    expect(status?.getAttribute("role")).toBe("status");
    expect(status?.getAttribute("aria-live")).toBe("polite");
    expect(api.verifyDeploymentAuditChain).toHaveBeenCalledWith("research-deployment");
  });
});
