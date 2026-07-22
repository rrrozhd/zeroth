"use client";

import { createContext, useContext, useState, useCallback, useRef } from "react";

type Toast = { id: number; msg: string };

const Ctx = createContext<(msg: string) => void>(() => {});
export const useToast = () => useContext(Ctx);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const seq = useRef(0);
  const push = useCallback((msg: string) => {
    const id = ++seq.current;
    setItems((x) => [...x, { id, msg }]);
    setTimeout(() => setItems((x) => x.filter((t) => t.id !== id)), 3200);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div
        style={{
          position: "fixed",
          right: 16,
          bottom: 16,
          display: "flex",
          flexDirection: "column",
          gap: 8,
          zIndex: 60,
        }}
      >
        {items.map((t) => (
          <div
            key={t.id}
            className="z-fade"
            style={{
              background: "var(--bg-raised-2)",
              border: "1px solid rgba(94,234,212,0.35)",
              borderRadius: 8,
              padding: "9px 12px",
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              color: "var(--text-primary)",
              maxWidth: 360,
            }}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
