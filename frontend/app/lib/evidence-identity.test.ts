// @vitest-environment jsdom

import { describe, expect, it } from "vitest";

import { assignEvidenceIdentities, evidenceIdentityOf } from "./evidence-identity";

describe("evidence control identities", () => {
  it("assigns stable route-and-accessible-name identities to every interactive control", () => {
    document.body.innerHTML = `
      <main>
        <label for="artifact-id">Artifact ID</label>
        <input id="artifact-id" />
        <button>Load artifact</button>
        <a href="/console/runs/">Runs</a>
        <details><summary>Inspect manifest</summary></details>
        <div role="region" tabindex="0" aria-label="Candidate table"></div>
      </main>
    `;

    const result = assignEvidenceIdentities(document.body, "/console/artifacts/");

    expect(result.errors).toEqual([]);
    expect(evidenceIdentityOf(document.querySelector("input")!)).toBe(
      "artifacts.input.artifact-id",
    );
    expect(evidenceIdentityOf(document.querySelector("button")!)).toBe(
      "artifacts.button.load-artifact",
    );
    expect(evidenceIdentityOf(document.querySelector("a")!)).toBe("artifacts.link.runs");
    expect(evidenceIdentityOf(document.querySelector("summary")!)).toBe(
      "artifacts.summary.inspect-manifest",
    );
    expect(evidenceIdentityOf(document.querySelector("[role=region]")!)).toBe(
      "artifacts.region.candidate-table",
    );
  });

  it("reports unnamed and duplicate controls instead of inventing order-based identities", () => {
    document.body.innerHTML = `
      <main>
        <button aria-label="Retry"></button>
        <button aria-label="Retry"></button>
        <button></button>
      </main>
    `;

    const result = assignEvidenceIdentities(document.body, "/console/audit/");

    expect(result.errors).toEqual([
      "duplicate evidence identity: audit.button.retry",
      "interactive control has no accessible evidence name: button",
    ]);
    expect(document.querySelectorAll("[data-evidence-id]")).toHaveLength(2);
  });

  it("uses explicit semantic scopes to distinguish repeated row and section actions", () => {
    document.body.innerHTML = `
      <main>
        <section data-evidence-scope="metrics">
          <button>Refresh</button>
        </section>
        <section data-evidence-scope="manifests">
          <button>Refresh</button>
        </section>
        <div data-evidence-scope="connector-chroma">
          <button>Test</button>
        </div>
        <div data-evidence-scope="connector-ephemeral">
          <button>Test</button>
        </div>
      </main>
    `;

    const result = assignEvidenceIdentities(document.body, "/console/connectors/");

    expect(result.errors).toEqual([]);
    expect(
      Array.from(document.querySelectorAll("button"), (button) => evidenceIdentityOf(button)),
    ).toEqual([
      "connectors.metrics.button.refresh",
      "connectors.manifests.button.refresh",
      "connectors.connector-chroma.button.test",
      "connectors.connector-ephemeral.button.test",
    ]);
  });

  it("preserves a deliberate explicit identity", () => {
    document.body.innerHTML = `<button data-evidence-id="runs.action.cancel">Cancel</button>`;

    const result = assignEvidenceIdentities(document.body, "/console/runs/");

    expect(result.errors).toEqual([]);
    expect(evidenceIdentityOf(document.querySelector("button")!)).toBe("runs.action.cancel");
  });

  it("recomputes generated identities after label, scope, and route changes", () => {
    document.body.innerHTML = `
      <section data-evidence-scope="queue"><button>Loading</button></section>
    `;
    const button = document.querySelector("button")!;

    assignEvidenceIdentities(document.body, "/console/studio/");
    expect(evidenceIdentityOf(button)).toBe("studio.queue.button.loading");

    button.textContent = "Refresh";
    button.parentElement!.dataset.evidenceScope = "workflows";
    assignEvidenceIdentities(document.body, "/console/runs/");
    expect(evidenceIdentityOf(button)).toBe("runs.workflows.button.refresh");
  });

  it("keeps authored identities fixed when their accessible text changes", () => {
    document.body.innerHTML = `<button data-evidence-id="runs.refresh">Loading</button>`;
    const button = document.querySelector("button")!;

    assignEvidenceIdentities(document.body, "/console/runs/");
    button.textContent = "Refresh";
    assignEvidenceIdentities(document.body, "/console/audit/");

    expect(evidenceIdentityOf(button)).toBe("runs.refresh");
  });

  it("hashes truncated labels so long common prefixes remain distinct", () => {
    const prefix = "a".repeat(90);
    document.body.innerHTML = `<button>${prefix}-left</button><button>${prefix}-right</button>`;

    const result = assignEvidenceIdentities(document.body, "/console/studio/");
    const identities = Array.from(document.querySelectorAll("button"), evidenceIdentityOf);

    expect(result.errors).toEqual([]);
    expect(new Set(identities).size).toBe(2);
    expect(identities.every((identity) => identity!.length <= 86)).toBe(true);
  });
});
