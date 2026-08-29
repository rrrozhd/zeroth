// @vitest-environment jsdom

import type { AnchorHTMLAttributes, ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/runs/",
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...props}>{children as ReactNode}</a>
  ),
}));

import { Sidebar } from "./Sidebar";

describe("Sidebar navigation states", () => {
  it("uses the Zeroth pixel-D brand mark in the product lockup", () => {
    const markup = renderToStaticMarkup(<Sidebar collapsed={false} />);
    const document = new DOMParser().parseFromString(markup, "text/html");
    const mark = document.querySelector<HTMLImageElement>("img.console-sidebar-brand-mark");

    expect(mark?.getAttribute("src")).toBe("/console/zeroth-mark.png");
    expect(mark?.getAttribute("width")).toBe("28");
    expect(mark?.getAttribute("height")).toBe("28");
    expect(mark?.getAttribute("aria-hidden")).toBe("true");
  });

  it("marks the current route and exposes a shared class for hover and focus styling", () => {
    const markup = renderToStaticMarkup(<Sidebar collapsed={false} />);
    const document = new DOMParser().parseFromString(markup, "text/html");
    const navigation = document.querySelector("nav");
    const runs = document.querySelector<HTMLAnchorElement>('a[href="/runs"]');
    const overview = document.querySelector<HTMLAnchorElement>('a[href="/"]');

    expect(navigation?.classList.contains("console-sidebar-nav")).toBe(true);
    expect(runs?.classList.contains("console-sidebar-link")).toBe(true);
    expect(runs?.classList.contains("is-active")).toBe(true);
    expect(runs?.getAttribute("aria-current")).toBe("page");
    expect(overview?.className).toBe("console-sidebar-link");
    expect(overview?.hasAttribute("aria-current")).toBe(false);
  });
});
