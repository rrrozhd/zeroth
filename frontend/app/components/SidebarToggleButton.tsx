"use client";

import { useId } from "react";

export function SidebarToggleButton({
  collapsed,
  onToggle,
  className,
}: {
  collapsed: boolean;
  onToggle: () => void;
  className?: string;
}) {
  const label = collapsed ? "Expand navigation" : "Collapse navigation";
  const tooltipId = useId();

  return (
    <span className="shortcut-tooltip-anchor shortcut-tooltip-anchor--start">
      <button
        type="button"
        className={["console-sidebar-toggle", className].filter(Boolean).join(" ")}
        aria-label={label}
        aria-describedby={tooltipId}
        aria-expanded={!collapsed}
        onClick={onToggle}
      >
        <svg aria-hidden viewBox="0 0 12 20">
          <path d={collapsed ? "M3 3l5 7-5 7" : "M8 3l-5 7 5 7"} />
        </svg>
      </button>
      <span id={tooltipId} className="shortcut-tooltip" role="tooltip">
        {label}<kbd>Ctrl/⌘ B</kbd>
      </span>
    </span>
  );
}
