import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { SidebarToggleButton } from "./SidebarToggleButton";

describe("SidebarToggleButton", () => {
  it.each([
    { collapsed: true, label: "Expand navigation", expanded: "false" },
    { collapsed: false, label: "Collapse navigation", expanded: "true" },
  ])("describes the navigation state when collapsed=$collapsed", ({ collapsed, label, expanded }) => {
    const html = renderToStaticMarkup(
      <SidebarToggleButton collapsed={collapsed} onToggle={vi.fn()} />,
    );

    expect(html).toContain(`aria-label="${label}"`);
    expect(html).toContain(`aria-expanded="${expanded}"`);
    expect(html).toContain("aria-describedby=");
    expect(html).toContain('role="tooltip"');
    expect(html).toContain(`${label}<kbd>Ctrl/⌘ B</kbd>`);
    expect(html).not.toContain("title=");
    expect(html).toContain("console-sidebar-toggle");
  });
});
