// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const navigation = vi.hoisted(() => ({ pathname: "/studio/edit" }));

vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
}));

vi.mock("./Sidebar", () => ({
  Sidebar: ({ collapsed }: { collapsed: boolean }) => (
    <aside data-collapsed={String(collapsed)}>Navigation</aside>
  ),
}));

vi.mock("./Topbar", () => ({
  Topbar: () => <header>Topbar</header>,
}));

vi.mock("./Toast", () => ({
  ToastProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("@/app/lib/regulus", () => ({
  detectRegulus: vi.fn().mockResolvedValue("disabled"),
}));

vi.mock("@/app/hooks/usePolling", () => ({
  usePolling: vi.fn(),
}));

vi.mock("@/app/lib/api", () => ({
  listApprovals: vi.fn().mockResolvedValue([]),
}));

import { AppShell } from "./AppShell";

let container: HTMLDivElement;
let root: Root;

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
  window.localStorage.clear();
  window.localStorage.setItem("zeroth.sidebarCollapsed", "true");
  navigation.pathname = "/studio/edit";
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false }),
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("AppShell Studio shortcuts", () => {
  it("does not animate the editor main that owns a fixed-position canvas", async () => {
    await act(async () => {
      root.render(<AppShell><div>Canvas</div></AppShell>);
    });

    expect(container.querySelector("main")?.classList.contains("z-fade")).toBe(false);
  });

  it("does not animate the shell main because native Safari can leave the layer unpainted", async () => {
    navigation.pathname = "/runs";
    await act(async () => {
      root.render(<AppShell><div>Runs</div></AppShell>);
    });

    expect(container.querySelector("main")?.classList.contains("z-fade")).toBe(false);
  });

  it("installs route-scoped evidence identities for shell and page controls", async () => {
    await act(async () => {
      root.render(<AppShell><button>Run workflow</button></AppShell>);
    });

    await waitFor(() => {
      const button = Array.from(container.querySelectorAll("button")).find(
        (candidate) => candidate.textContent === "Run workflow",
      );
      expect(button?.getAttribute("data-evidence-id")).toBe(
        "studio.button.run-workflow",
      );
    });
  });

  it("starts compact editors collapsed even after a desktop-expanded preference", async () => {
    window.localStorage.setItem("zeroth.sidebarCollapsed", "false");
    vi.mocked(window.matchMedia).mockReturnValue({ matches: true } as MediaQueryList);

    await act(async () => {
      root.render(<AppShell><div>Canvas</div></AppShell>);
    });

    await waitFor(() => {
      expect(container.querySelector("aside")?.getAttribute("data-collapsed")).toBe("true");
    });
  });

  it("collapses an open editor navigation when the viewport becomes compact", async () => {
    window.localStorage.setItem("zeroth.sidebarCollapsed", "false");
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    let compact = false;
    const editorQuery = {
      get matches() {
        return compact;
      },
      media: "(max-width: 900px)",
      onchange: null,
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.add(listener),
      removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) =>
        listeners.delete(listener),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    } as MediaQueryList;
    vi.mocked(window.matchMedia).mockImplementation((query) =>
      query === "(max-width: 900px)"
        ? editorQuery
        : ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList),
    );

    await act(async () => {
      root.render(<AppShell><div>Canvas</div></AppShell>);
    });
    await waitFor(() => {
      expect(container.querySelector("aside")?.getAttribute("data-collapsed")).toBe("false");
    });

    compact = true;
    await act(async () => {
      listeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent));
    });

    expect(container.querySelector("aside")?.getAttribute("data-collapsed")).toBe("true");
  });

  it("starts every phone surface collapsed so content is not hidden behind the navigation", async () => {
    navigation.pathname = "/retention";
    window.localStorage.setItem("zeroth.sidebarCollapsed", "false");
    vi.mocked(window.matchMedia).mockImplementation(
      (query) => ({ matches: query === "(max-width: 560px)" }) as MediaQueryList,
    );

    await act(async () => {
      root.render(<AppShell><div>Retention</div></AppShell>);
    });

    await waitFor(() => {
      expect(container.querySelector("aside")?.getAttribute("data-collapsed")).toBe("true");
    });
  });

  it("keeps ordinary tablet and desktop surfaces expanded by default", async () => {
    navigation.pathname = "/retention";
    window.localStorage.setItem("zeroth.sidebarCollapsed", "false");
    vi.mocked(window.matchMedia).mockReturnValue({ matches: false } as MediaQueryList);

    await act(async () => {
      root.render(<AppShell><div>Retention</div></AppShell>);
    });

    await waitFor(() => {
      expect(container.querySelector("aside")?.getAttribute("data-collapsed")).toBe("false");
    });
  });

  it("toggles the navigation with Ctrl/Cmd+B and persists the state", async () => {
    await act(async () => {
      root.render(<AppShell><div>Canvas</div></AppShell>);
    });
    await waitFor(() => {
      expect(container.querySelector('[aria-label="Expand navigation"]')).toBeTruthy();
    });

    const expand = new KeyboardEvent("keydown", {
      key: "b",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      document.dispatchEvent(expand);
    });

    expect(expand.defaultPrevented).toBe(true);
    expect(container.querySelector('[aria-label="Collapse navigation"]')).toBeTruthy();
    expect(window.localStorage.getItem("zeroth.sidebarCollapsed")).toBe("false");

    const collapse = new KeyboardEvent("keydown", {
      key: "b",
      metaKey: true,
      bubbles: true,
      cancelable: true,
    });
    await act(async () => {
      document.dispatchEvent(collapse);
    });

    expect(collapse.defaultPrevented).toBe(true);
    expect(container.querySelector('[aria-label="Expand navigation"]')).toBeTruthy();
    expect(window.localStorage.getItem("zeroth.sidebarCollapsed")).toBe("true");
  });
});
