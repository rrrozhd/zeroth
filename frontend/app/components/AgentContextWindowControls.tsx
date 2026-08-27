"use client";

import { useEffect, useState } from "react";

import { consoleControlClassName } from "@/app/components/primitives";

export type AgentContextWindowSettings = {
  max_context_tokens: number;
  summary_trigger_ratio: number;
  compaction_strategy: string;
  preserve_recent_messages_count: number;
  archive_originals: boolean;
};

const DEFAULTS: AgentContextWindowSettings = {
  max_context_tokens: 128_000,
  summary_trigger_ratio: 0.8,
  compaction_strategy: "observation_masking",
  preserve_recent_messages_count: 4,
  archive_originals: false,
};

const STRATEGIES = [
  {
    value: "observation_masking",
    label: "Observation masking",
    hint: "Replace older tool outputs with compact token-count markers.",
  },
  {
    value: "truncation",
    label: "Truncation",
    hint: "Drop the oldest middle turns while keeping the system message and recent turns.",
  },
  {
    value: "llm_summarization",
    label: "LLM summarization",
    hint: "Condense older turns into one summary using this agent's configured provider.",
  },
] as const;

function settingsFrom(value: unknown): AgentContextWindowSettings | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Partial<AgentContextWindowSettings>;
  return {
    max_context_tokens:
      typeof raw.max_context_tokens === "number"
        ? raw.max_context_tokens
        : DEFAULTS.max_context_tokens,
    summary_trigger_ratio:
      typeof raw.summary_trigger_ratio === "number"
        ? raw.summary_trigger_ratio
        : DEFAULTS.summary_trigger_ratio,
    compaction_strategy:
      typeof raw.compaction_strategy === "string"
        ? raw.compaction_strategy
        : DEFAULTS.compaction_strategy,
    preserve_recent_messages_count:
      typeof raw.preserve_recent_messages_count === "number"
        ? raw.preserve_recent_messages_count
        : DEFAULTS.preserve_recent_messages_count,
    archive_originals:
      typeof raw.archive_originals === "boolean"
        ? raw.archive_originals
        : DEFAULTS.archive_originals,
  };
}

function BoundedNumberControl({
  evidenceId,
  label,
  hint,
  value,
  min,
  exclusiveMin = false,
  max,
  step,
  integer,
  readOnly,
  onValidChange,
}: {
  evidenceId: string;
  label: string;
  hint: string;
  value: number;
  min: number;
  exclusiveMin?: boolean;
  max?: number;
  step: number | "any";
  integer: boolean;
  readOnly: boolean;
  onValidChange: (value: number) => void;
}) {
  const [raw, setRaw] = useState(String(value));
  const [error, setError] = useState<string | null>(null);
  const hintId = `${evidenceId}.hint`;
  const errorId = `${evidenceId}.error`;

  useEffect(() => {
    setRaw(String(value));
    setError(null);
  }, [value]);

  function validate(nextRaw: string): number | null {
    if (nextRaw.trim() === "") {
      setError("Enter a value. The last valid value remains active.");
      return null;
    }
    const parsed = Number(nextRaw);
    const belowMinimum = exclusiveMin ? parsed <= min : parsed < min;
    const outside =
      !Number.isFinite(parsed) || belowMinimum || (max !== undefined && parsed > max);
    if (outside || (integer && !Number.isInteger(parsed))) {
      setError(
        integer
          ? "Use a whole number of 0 or more. Fix this value before changing context settings again; the last valid value remains active."
          : "Use a ratio greater than 0 and no more than 1. Fix this value before changing context settings again; the last valid value remains active.",
      );
      return null;
    }
    setError(null);
    return parsed;
  }

  return (
    <label className="block text-xs">
      <span className="mb-1 block font-medium">{label}</span>
      <input
        data-evidence-id={evidenceId}
        type="number"
        inputMode={integer ? "numeric" : "decimal"}
        value={raw}
        min={min}
        max={max}
        step={step}
        required
        disabled={readOnly}
        aria-invalid={error ? "true" : "false"}
        aria-describedby={`${hintId}${error ? ` ${errorId}` : ""}`}
        onChange={(event) => {
          const nextRaw = event.target.value;
          setRaw(nextRaw);
          const parsed = validate(nextRaw);
          if (parsed !== null) onValidChange(parsed);
        }}
        className={consoleControlClassName}
      />
      <span id={hintId} className="mt-1 block font-normal text-muted">
        {hint}
      </span>
      {error && (
        <span
          id={errorId}
          role="alert"
          data-evidence-id={`${evidenceId}.validation-error`}
          className="mt-1 block font-normal text-danger"
        >
          {error}
        </span>
      )}
    </label>
  );
}

export function AgentContextWindowControls({
  value,
  readOnly,
  onChange,
}: {
  value: unknown;
  readOnly: boolean;
  onChange: (value: AgentContextWindowSettings | null) => void;
}) {
  const settings = settingsFrom(value);
  const enabled = settings !== null;
  const knownStrategy = STRATEGIES.some((strategy) => strategy.value === settings?.compaction_strategy);
  const selectedStrategy = STRATEGIES.find(
    (strategy) => strategy.value === settings?.compaction_strategy,
  );

  function patch(next: Partial<AgentContextWindowSettings>) {
    if (!settings) return;
    onChange({ ...settings, ...next });
  }

  return (
    <fieldset
      data-evidence-scope="agent-context-window"
      className="space-y-3 border-t border-border pt-4"
    >
      <legend className="text-sm font-medium">Context window</legend>
      <p className="text-xs leading-relaxed text-muted">
        Bound long-running threads before the model call. Compaction state and counts appear in the
        node&apos;s audit timeline.
      </p>
      <label className="flex items-start gap-2 text-xs">
        <input
          data-evidence-id="studio.agent.context-window.enabled"
          type="checkbox"
          checked={enabled}
          disabled={readOnly}
          onChange={(event) => onChange(event.target.checked ? { ...DEFAULTS } : null)}
          className="mt-0.5"
        />
        <span>
          <span className="block font-medium">Enable context management</span>
          <span className="mt-0.5 block text-muted">
            Stores explicit per-agent limits. Turning this off removes the nested configuration.
          </span>
        </span>
      </label>

      {settings && (
        <div className="space-y-3 pl-6">
          <BoundedNumberControl
            evidenceId="studio.agent.context-window.max-context-tokens"
            label="Maximum context tokens"
            hint="0 disables automatic compaction while retaining the authored policy; otherwise use the model's supported window."
            value={settings.max_context_tokens}
            min={0}
            step={1}
            integer
            readOnly={readOnly}
            onValidChange={(next) => patch({ max_context_tokens: next })}
          />

          <BoundedNumberControl
            evidenceId="studio.agent.context-window.trigger-ratio"
            label="Compaction trigger ratio"
            hint="Compact when estimated usage reaches this share of the maximum. Greater than 0, up to 1."
            value={settings.summary_trigger_ratio}
            min={0}
            exclusiveMin
            max={1}
            step="any"
            integer={false}
            readOnly={readOnly}
            onValidChange={(next) => patch({ summary_trigger_ratio: next })}
          />

          <label className="block text-xs">
            <span className="mb-1 block font-medium">Compaction strategy</span>
            <select
              data-evidence-id="studio.agent.context-window.strategy"
              value={settings.compaction_strategy}
              disabled={readOnly}
              onChange={(event) => patch({ compaction_strategy: event.target.value })}
              className={consoleControlClassName}
            >
              {!knownStrategy && (
                <option value={settings.compaction_strategy}>
                  {settings.compaction_strategy} (unsupported)
                </option>
              )}
              {STRATEGIES.map((strategy) => (
                <option key={strategy.value} value={strategy.value}>
                  {strategy.label}
                </option>
              ))}
            </select>
            <span className="mt-1 block font-normal text-muted">
              {selectedStrategy?.hint ?? "Choose a supported strategy before publishing."}
            </span>
          </label>

          {settings.compaction_strategy === "llm_summarization" && (
            <p
              data-evidence-id="studio.agent.context-window.llm-summarization-notice"
              className="text-xs leading-relaxed text-muted"
            >
              Summarization makes an additional instrumented provider call and remains subject to the
              run&apos;s capability and cost ceilings.
            </p>
          )}

          <BoundedNumberControl
            evidenceId="studio.agent.context-window.preserve-recent-messages"
            label="Recent messages to preserve"
            hint="Keep this many newest messages untouched during compaction. 0 preserves none."
            value={settings.preserve_recent_messages_count}
            min={0}
            step={1}
            integer
            readOnly={readOnly}
            onValidChange={(next) => patch({ preserve_recent_messages_count: next })}
          />

          <label className="flex items-start gap-2 text-xs">
            <input
              data-evidence-id="studio.agent.context-window.archive-originals"
              type="checkbox"
              checked={settings.archive_originals}
              disabled={readOnly}
              onChange={(event) => patch({ archive_originals: event.target.checked })}
              className="mt-0.5"
            />
            <span>
              <span className="block font-medium">Archive compacted originals</span>
              <span className="mt-0.5 block text-muted">
                Retain dropped or masked messages in compaction state for recovery. Retention policy
                still applies.
              </span>
            </span>
          </label>
        </div>
      )}
    </fieldset>
  );
}
