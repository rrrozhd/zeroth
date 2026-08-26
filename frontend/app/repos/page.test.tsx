// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listRepoInstallations: vi.fn(),
  claimRepoInstallation: vi.fn(),
  listInstallationRepositories: vi.fn(),
  resolveRepoRef: vi.fn(),
  createRepoCheckout: vi.fn(),
  getRepoCheckout: vi.fn(),
  getCheckoutAttestation: vi.fn(),
  createRepoRun: vi.fn(),
  getRepoRun: vi.fn(),
  getRepoRunEvidence: vi.fn(),
}));

vi.mock("@/app/lib/api", async () => ({
  ...(await vi.importActual<typeof import("@/app/lib/api")>("@/app/lib/api")),
  ...api,
}));

import { ApiError } from "@/app/lib/api";
import ReposPage from "./page";

const COMMIT_SHA = "a".repeat(40);

const INSTALLATION = {
  installation_id: 42,
  account_login: "acme",
  account_type: "Organization",
  repository_selection: "selected",
  status: "active",
  last_verified_at: null,
  suspended_at: null,
  revoked_at: null,
  created_at: "2026-08-25T12:00:00Z",
  updated_at: "2026-08-25T12:00:00Z",
};

const REPOSITORY = {
  repository_id: 7,
  owner: "acme",
  name: "pipelines",
  full_name: "acme/pipelines",
  private: true,
  default_branch: "main",
  status: "active",
  added_at: "2026-08-25T12:00:00Z",
  removed_at: null,
};

const CHECKOUT = {
  checkout_id: "co-1",
  installation_id: 42,
  repository_id: 7,
  repository_full_name: "acme/pipelines",
  requested_ref: "main",
  state: "staged",
  resolved_commit_sha: COMMIT_SHA,
  git_tree_id: "b".repeat(40),
  tree_digest: "sha256:" + "c".repeat(64),
  config_digest: "sha256:" + "d".repeat(64),
  manifest_digest: "sha256:" + "e".repeat(64),
  script_name: "smoke.py",
  failure_code: null,
  failure_detail: null,
  file_count: 12,
  size_bytes: 4096,
  expires_at: null,
  created_at: "2026-08-25T12:01:00Z",
  updated_at: "2026-08-25T12:01:00Z",
  attestation_present: true,
  validation_report: null,
};

const RUN_PENDING = {
  run_id: "run-1",
  checkout_id: "co-1",
  script_name: "smoke.py",
  state: "pending",
  exit_code: null,
  failure_code: null,
  smoke_passed: null,
  output_payload: null,
  created_at: "2026-08-25T12:02:00Z",
  started_at: null,
  finished_at: null,
  updated_at: "2026-08-25T12:02:00Z",
};

const RUN_SUCCEEDED = {
  ...RUN_PENDING,
  state: "succeeded",
  exit_code: 0,
  smoke_passed: true,
  started_at: "2026-08-25T12:02:01Z",
  finished_at: "2026-08-25T12:02:05Z",
  updated_at: "2026-08-25T12:02:05Z",
};

const EVIDENCE = {
  run: RUN_SUCCEEDED,
  checkout_attestation: {
    payload: {
      schema_version: 1,
      tenant_id: "default",
      workspace_id: null,
      checkout_id: "co-1",
      installation_id: 42,
      repository_id: 7,
      repository_full_name: "acme/pipelines",
      requested_ref: "main",
      commit_sha: COMMIT_SHA,
      git_tree_id: "b".repeat(40),
      tree_digest: "sha256:" + "c".repeat(64),
      config_digest: "sha256:" + "d".repeat(64),
      manifest_digest: "sha256:" + "e".repeat(64),
      script_name: "smoke.py",
      issued_at: "2026-08-25T12:01:00Z",
    },
    attestation_digest: "f".repeat(64),
    attestation_signature: null,
    attestation_key_id: null,
    attestation_algorithm: null,
    digest_verified: true,
    signature_verified: null,
    verified: true,
  },
  audits: [],
  summary: {
    approval_count: 0,
    audit_count: 3,
    memory_interaction_count: 0,
    tool_call_count: 1,
  },
  policy_events: ["econ.budget.checked"],
};

let container: HTMLDivElement;
let root: Root;

function buttons(): HTMLButtonElement[] {
  return Array.from(container.querySelectorAll("button"));
}

function buttonWithText(text: string): HTMLButtonElement | undefined {
  return buttons().find((b) => b.textContent?.includes(text));
}

function setNativeValue(
  element: HTMLInputElement | HTMLTextAreaElement,
  value: string,
) {
  const proto =
    element instanceof window.HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
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

async function mountPage() {
  await act(async () => root.render(<ReposPage />));
  await waitFor(() => expect(container.textContent).toContain("acme"));
}

/** Walk the selection flow down to a selected repository. */
async function selectRepository() {
  await act(async () => buttonWithText("acme")?.click());
  await waitFor(() => expect(container.textContent).toContain("acme/pipelines"));
  await act(async () => buttonWithText("acme/pipelines")?.click());
  await waitFor(() => expect(buttonWithText("Create checkout")).toBeTruthy());
}

function refInput(): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>('input[placeholder="main"]');
  expect(input).toBeTruthy();
  return input!;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  window.localStorage.setItem("zeroth.apiKey", "operator-key");
  api.listRepoInstallations.mockResolvedValue([INSTALLATION]);
  api.listInstallationRepositories.mockResolvedValue([REPOSITORY]);
  api.claimRepoInstallation.mockResolvedValue(INSTALLATION);
  api.resolveRepoRef.mockResolvedValue({ commit_sha: COMMIT_SHA });
  api.createRepoCheckout.mockResolvedValue(CHECKOUT);
  api.createRepoRun.mockResolvedValue(RUN_PENDING);
  api.getRepoRun.mockResolvedValue(RUN_SUCCEEDED);
  api.getRepoRunEvidence.mockResolvedValue(EVIDENCE);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("repos page", () => {
  it("renders the four cards and populates installations", async () => {
    await mountPage();

    for (const title of ["Installations", "Checkout", "Run", "Provenance"]) {
      expect(container.textContent).toContain(title);
    }
    expect(api.listRepoInstallations).toHaveBeenCalled();
    expect(container.textContent).toContain("acme");
    expect(container.textContent).toContain("selected repositories");
  });

  it("claims an installation through the claim form", async () => {
    await mountPage();

    const input = container.querySelector<HTMLInputElement>(
      'input[placeholder="e.g. 43512286"]',
    );
    expect(input).toBeTruthy();
    await act(async () => setNativeValue(input!, "123"));
    await act(async () => buttonWithText("Claim")?.click());

    await waitFor(() => expect(api.claimRepoInstallation).toHaveBeenCalledWith(123));
    // The list is reloaded after a successful claim.
    expect(api.listRepoInstallations.mock.calls.length).toBeGreaterThan(1);
  });

  it("resolves a ref and shows the pinned commit SHA", async () => {
    await mountPage();
    await selectRepository();

    await act(async () => setNativeValue(refInput(), "main"));
    await act(async () => buttonWithText("Resolve")?.click());

    await waitFor(() => expect(container.textContent).toContain(COMMIT_SHA));
    expect(api.resolveRepoRef).toHaveBeenCalledWith(7, "main");
  });

  it("renders manifest validation issues from a checkout 422", async () => {
    api.createRepoCheckout.mockRejectedValue(
      new ApiError(422, "manifest validation failed", {
        code: "manifest_validation_failed",
        checkout_id: "co-refused",
        issues: [
          {
            severity: "error",
            code: "script_not_declared",
            path: ["scripts", "run"],
            message: "the manifest declares no runnable script",
          },
        ],
      }),
    );
    await mountPage();
    await selectRepository();

    await act(async () => setNativeValue(refInput(), "main"));
    await act(async () => buttonWithText("Create checkout")?.click());

    await waitFor(() => expect(container.textContent).toContain("script_not_declared"));
    expect(container.textContent).toContain("scripts.run");
    expect(container.textContent).toContain("the manifest declares no runnable script");
    expect(api.createRepoCheckout).toHaveBeenCalledWith(7, { ref: "main" });
  });

  it("runs the declared script and updates status from the poll", async () => {
    await mountPage();
    await selectRepository();

    await act(async () => setNativeValue(refInput(), "main"));
    await act(async () => buttonWithText("Create checkout")?.click());
    await waitFor(() => expect(container.textContent).toContain("smoke.py"));

    await act(async () => buttonWithText("Run")?.click());
    await waitFor(() => expect(api.createRepoRun).toHaveBeenCalled());
    expect(api.createRepoRun).toHaveBeenCalledWith("co-1", {
      script: "smoke.py",
      input_payload: {},
    });

    // The poll fires immediately on activation and lands the terminal state.
    await waitFor(() => expect(container.textContent).toContain("Succeeded"));
    expect(api.getRepoRun).toHaveBeenCalledWith("run-1");

    // A finished run auto-fetches the evidence bundle into the provenance card.
    await waitFor(() => expect(api.getRepoRunEvidence).toHaveBeenCalledWith("run-1"));
    await waitFor(() => expect(container.textContent).toContain("econ.budget.checked"));
    expect(container.textContent).toContain("verified");
  });

  it("refuses invalid JSON input client-side without calling the API", async () => {
    await mountPage();
    await selectRepository();

    await act(async () => setNativeValue(refInput(), "main"));
    await act(async () => buttonWithText("Create checkout")?.click());
    await waitFor(() => expect(container.textContent).toContain("smoke.py"));

    const textarea = container.querySelector("textarea");
    expect(textarea).toBeTruthy();
    await act(async () => setNativeValue(textarea!, "{not json"));
    await act(async () => buttonWithText("Run")?.click());

    await waitFor(() =>
      expect(container.textContent).toContain("Input isn't valid JSON"),
    );
    expect(api.createRepoRun).not.toHaveBeenCalled();
  });
});
