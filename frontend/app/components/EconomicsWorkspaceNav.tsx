"use client";

import Link from "next/link";
import { ECONOMICS_VIEWS } from "./nav";
import styles from "./EconomicsWorkspaceNav.module.css";

type EconomicsView = (typeof ECONOMICS_VIEWS)[number]["id"];

export function EconomicsWorkspaceNav({ active }: { active: EconomicsView }) {
  return (
    <nav className={styles.nav} aria-label="Economics views">
      {ECONOMICS_VIEWS.map((view) => {
        const current = active === view.id;
        return (
          <Link
            key={view.href}
            href={view.href}
            className={`${styles.link}${current ? ` ${styles.active}` : ""}`}
            aria-current={current ? "page" : undefined}
          >
            {view.label}
          </Link>
        );
      })}
    </nav>
  );
}
