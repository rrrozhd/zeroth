import { useEffect, useRef } from "react";

export function computeNextDelay({
  active,
  hidden,
  base,
}: {
  active: boolean;
  hidden: boolean;
  base: number;
}): number | null {
  if (!active) return null;
  return hidden ? base * 4 : base;
}

/** Calls `fn` every `base` ms while `active`; pauses when inactive, backs off 4x
 *  when the tab is hidden. Fires once immediately on activation. */
export function usePolling(fn: () => void, base: number, active: boolean) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (!active) return;
    saved.current();
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const delay = computeNextDelay({ active, hidden: document.hidden, base });
      if (delay == null) return;
      timer = setTimeout(() => {
        saved.current();
        tick();
      }, delay);
    };
    tick();
    return () => clearTimeout(timer);
  }, [active, base]);
}
