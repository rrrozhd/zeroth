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
              background: "var(--bg-card)",
              border: "1px solid var(--hair-strong)",
              borderRadius: 8,
              padding: "9px 12px",
              fontFamily: "var(--font-sans)",
              fontSize: 12,
              color: "var(--text-primary)",
              maxWidth: 360,
              boxShadow: "0 2px 8px rgba(22,21,31,.08)",
            }}
          >
            {t.msg}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
