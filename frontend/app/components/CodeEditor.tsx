"use client";

import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import CodeMirror from "@uiw/react-codemirror";
import { useEffect, useState } from "react";

// Python editor for the code node's inline source. Loaded lazily (next/dynamic
// in NodeInspector) so CodeMirror stays out of every other page's bundle.
export default function CodeEditor({
  value,
  readOnly = false,
  language = "python",
  height = "280px",
  onChange,
}: {
  value: string;
  readOnly?: boolean;
  language?: "python" | "json";
  height?: string;
  onChange: (v: string) => void;
}) {
  // Follow the OS color scheme, live — the console theme does the same.
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    setDark(mq.matches);
    const onChangeScheme = (e: MediaQueryListEvent) => setDark(e.matches);
    mq.addEventListener("change", onChangeScheme);
    return () => mq.removeEventListener("change", onChangeScheme);
  }, []);

  return (
    <div className="overflow-hidden rounded-lg border border-border text-[13px]">
      <CodeMirror
        value={value}
        readOnly={readOnly}
        editable={!readOnly}
        onChange={onChange}
        extensions={[language === "json" ? json() : python()]}
        theme={dark ? "dark" : "light"}
        height={height}
        basicSetup={{
          lineNumbers: true,
          foldGutter: false,
          highlightActiveLine: !readOnly,
          autocompletion: true,
        }}
      />
    </div>
  );
}
