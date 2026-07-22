"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errMsg } from "@/app/lib/api";

export type Loadable<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
};

/** Fetch `fn` once on mount; `reload()` refetches. Keeps the last good data
 *  visible across a reload so only the first paint shows skeletons. Never throws
 *  — failures surface as `error` (via `errMsg`), so screens degrade to an inline
 *  error state instead of an error boundary. */
export function useLoad<T>(fn: () => Promise<T>): Loadable<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fnRef
      .current()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(errMsg(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}
