// @vitest-environment jsdom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { EvidenceIdentityObserver } from "./EvidenceIdentityObserver";

describe("EvidenceIdentityObserver", () => {
  let root: Root | null = null;

  afterEach(() => {
    act(() => root?.unmount());
    root = null;
    document.body.innerHTML = "";
  });

  it("labels controls added after the first render", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root!.render(<EvidenceIdentityObserver pathname="/console/artifacts/" />);
    });
    const button = document.createElement("button");
    button.textContent = "Refresh artifacts";
    host.appendChild(button);
    await act(async () => await Promise.resolve());

    expect(button.dataset.evidenceId).toBe("artifacts.button.refresh-artifacts");
  });

  it("relabels generated controls after text, scope, and pathname changes", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const scope = document.createElement("section");
    scope.dataset.evidenceScope = "queue";
    const button = document.createElement("button");
    button.textContent = "Loading";
    scope.appendChild(button);
    document.body.appendChild(scope);
    root = createRoot(host);

    await act(async () => {
      root!.render(<EvidenceIdentityObserver pathname="/console/studio/" />);
      await Promise.resolve();
    });
    expect(button.dataset.evidenceId).toBe("studio.queue.button.loading");

    await act(async () => {
      button.textContent = "Refresh";
      scope.dataset.evidenceScope = "workflows";
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(button.dataset.evidenceId).toBe("studio.workflows.button.refresh");

    await act(async () => {
      root!.render(<EvidenceIdentityObserver pathname="/console/runs/" />);
      await Promise.resolve();
    });
    expect(button.dataset.evidenceId).toBe("runs.workflows.button.refresh");
  });

  it("ignores interactive controls injected outside the console shell", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const injectedButton = document.createElement("button");
    injectedButton.setAttribute("aria-label", "Open framework dev tools");
    document.body.appendChild(injectedButton);

    await act(async () => {
      root!.render(
        <>
          <EvidenceIdentityObserver pathname="/console/artifacts/" />
          <div className="console-shell">
            <button>Refresh artifacts</button>
          </div>
        </>,
      );
      await Promise.resolve();
    });

    const productButton = host.querySelector("button")!;
    expect(productButton.dataset.evidenceId).toBe("artifacts.button.refresh-artifacts");
    expect(injectedButton.dataset.evidenceId).toBeUndefined();
  });
});
