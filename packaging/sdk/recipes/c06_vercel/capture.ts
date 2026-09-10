// Zeroth capture for Vercel AI SDK applications (recipe C06). No Zeroth package: this file and
// the application's own `ai` dependency are all it needs. Identity, metadata and pricing mirror
// zeroth.instrumentation (INTERFACE.md); delivery is the C07 worker's idempotent retry.
//
//   const capture = new Capture({ url, key, workflow, version, runId });
//   const result = await capture.call("answer", MODEL, () =>
//     generateText({ model, prompt, ...capture.callbacks("answer", MODEL) }));
//   capture.summary("completed", { total_usage: result.totalUsage });
//   capture.outcome(true, "final");
//   await capture.flush();   // await this before a serverless handler returns

const RATE_CARD_VERSION = "2026-09-10/litellm-1.98.0";
const RATES: Record<string, { provider: string; input: string; output: string; cacheRead: string; cacheWrite: string | null }> = {
  "gpt-4.1": { provider: "openai", input: "2.0", output: "8.0", cacheRead: "0.5", cacheWrite: null },
  "gpt-4.1-mini": { provider: "openai", input: "0.4", output: "1.6", cacheRead: "0.1", cacheWrite: null },
  "claude-sonnet-4-20250514": { provider: "anthropic", input: "3.0", output: "15.0", cacheRead: "0.3", cacheWrite: "3.75" },
  "claude-haiku-4-5-20251001": { provider: "anthropic", input: "1.0", output: "5.0", cacheRead: "0.1", cacheWrite: "1.25" },
};
const DATE_SUFFIX = /-(?:\d{4}-\d{2}-\d{2}|\d{8}|latest)$/;

export type Usage = { input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number; reasoning_tokens: number };
export type CaptureOptions = { url: string; key: string; workflow: string; version: string; runId: string; recordedAt?: string; retries?: number; timeoutMs?: number };
export type Report = { delivered: number; duplicates: number; lost: { id: string; reason: string }[]; attempts: Record<string, number> };

function unitsPerToken(rate: string): bigint {
  const [whole, fraction = ""] = rate.split(".");
  return BigInt(whole) * 100n + BigInt((fraction + "00").slice(0, 2));
}

function money(units: bigint): string {
  return `${units / 100_000_000n}.${(units % 100_000_000n).toString().padStart(8, "0")}`;
}

function bare(model: string): string {
  return model.includes("/") ? model.slice(model.indexOf("/") + 1) : model;
}

export function rates(model: string) {
  const name = bare(model);
  return RATES[name] ?? RATES[name.replace(DATE_SUFFIX, "")] ?? null;
}

export function price(model: string, usage: Usage): string | null {
  const card = rates(model);
  if (!card) return null;
  let units = BigInt(usage.input_tokens) * unitsPerToken(card.input) + BigInt(usage.output_tokens) * unitsPerToken(card.output);
  if (usage.cache_read_tokens) units += BigInt(usage.cache_read_tokens) * unitsPerToken(card.cacheRead);
  if (usage.cache_write_tokens) {
    if (card.cacheWrite === null) return null;
    units += BigInt(usage.cache_write_tokens) * unitsPerToken(card.cacheWrite);
  }
  return money(units);
}

/** The AI SDK's public usage object, normalised so input excludes cached tokens; null when absent. */
export function usageFromStep(usage: any): Usage | null {
  if (!usage) return null;
  const input = Number(usage.inputTokens ?? 0);
  const output = Number(usage.outputTokens ?? 0);
  if (!input && !output) return null;
  const cacheRead = Number(usage.inputTokenDetails?.cacheReadTokens ?? 0);
  const cacheWrite = Number(usage.inputTokenDetails?.cacheWriteTokens ?? 0);
  const noCache = usage.inputTokenDetails?.noCacheTokens;
  return {
    input_tokens: noCache !== undefined && noCache !== null ? Number(noCache) : Math.max(0, input - cacheRead - cacheWrite),
    output_tokens: output, cache_read_tokens: cacheRead, cache_write_tokens: cacheWrite,
    reasoning_tokens: Number(usage.outputTokenDetails?.reasoningTokens ?? 0),
  };
}

export class Capture {
  readonly report: Report = { delivered: 0, duplicates: 0, lost: [], attempts: {} };
  private queue: Promise<void> = Promise.resolve();
  private occurrences = new Map<string, number>();
  private charges = 0;
  private readonly o: Required<Omit<CaptureOptions, "recordedAt">> & { recordedAt?: string };

  constructor(options: CaptureOptions) {
    this.o = { retries: 3, timeoutMs: 5000, ...options };
  }

  /** A step name unique within the run: `answer`, `answer#2`, ... */
  step(name: string): string {
    const count = (this.occurrences.get(name) ?? 0) + 1;
    this.occurrences.set(name, count);
    return count === 1 ? name : `${name}#${count}`;
  }

  private eventId(step: string, attempt: number): string {
    return `${this.o.workflow}:${this.o.version}:${this.o.runId}:${step}:${attempt}`;
  }

  private now(): string {
    return this.o.recordedAt ?? new Date().toISOString();
  }

  /** One physical model call. Missing usage, an unlisted model or an error stays unmeasured. */
  charge(step: string, call: { model: string; usage: Usage | null; error?: string | null; requestId?: string | null; stream?: string | null; attempt?: number }): void {
    const model = bare(call.model);
    const card = rates(model);
    const cost = call.usage === null ? null : price(model, call.usage);
    const pricing = call.usage === null ? "missing_usage" : cost === null ? "unknown_model" : "rate_card";
    const attempt = call.attempt ?? 1;
    const id = this.eventId(step, attempt);
    this.charges += 1;
    this.post("/v1/executions", {
      workflow: this.o.workflow, workflow_version: this.o.version, run_id: this.o.runId, step, attempt,
      event_id: id, cost_role: "charge", charge_id: `charge:${id}`, recorded_at: this.now(), model_version: model,
      cost_usd: cost, cost_measurement: cost === null ? "unmeasured" : "estimated", latency_ms: 0,
      metadata: { charge_kind: "model", provider: card?.provider ?? "unknown", provider_request_id: call.requestId ?? null,
        usage: call.usage, pricing, rate_card_version: pricing === "rate_card" ? RATE_CARD_VERSION : null,
        error: call.error ?? null, framework: "vercel-ai", stream: call.stream ?? null },
    }, `${this.o.runId}:${step}:${attempt}`);
  }

  /** Callbacks to spread into generateText / streamText: every finished step is a charge. */
  callbacks(name: string, model: string) {
    let pending: string | null = null;
    return {
      onStepFinish: (step: any) => {
        pending = null;
        this.charge(this.step(name), { model: step.response?.modelId ?? model, usage: usageFromStep(step.usage),
          requestId: step.response?.id ?? null, stream: step.usage && (step as any).stream ? "complete" : null });
      },
      onAbort: () => { this.charge(pending ?? this.step(name), { model, usage: null, error: "aborted", stream: "aborted" }); pending = null; },
      onError: (event: any) => {
        const error = event?.error;
        this.charge(pending ?? this.step(name), { model, usage: null, error: error?.name ?? String(error), stream: "aborted" });
        pending = null;
      },
    };
  }

  /** Run a model call; if it rejects before any step was charged, charge the failed attempt. */
  async call<T>(name: string, model: string, fn: () => Promise<T>): Promise<T> {
    const before = this.charges;
    try {
      return await fn();
    } catch (error) {
      if (this.charges === before) {
        this.charge(this.step(name), { model, usage: null, error: (error as Error)?.name ?? String(error) });
      }
      throw error;
    }
  }

  tool(step: string, tool: string, costUsd: string): void {
    const id = this.eventId(step, 1);
    this.post("/v1/executions", {
      workflow: this.o.workflow, workflow_version: this.o.version, run_id: this.o.runId, step, attempt: 1,
      event_id: id, cost_role: "charge", charge_id: `charge:${id}`, recorded_at: this.now(), model_version: `tool:${tool}`,
      cost_usd: costUsd, cost_measurement: "measured", latency_ms: 0,
      metadata: { charge_kind: "tool", tool, pricing: "caller", error: null, framework: "vercel-ai" },
    }, `${this.o.runId}:${step}:1`);
  }

  /** The run aggregate: multi-step totals live here and never carry money. */
  summary(terminalState: string, extra: Record<string, unknown> = {}): void {
    this.post("/v1/executions", {
      workflow: this.o.workflow, workflow_version: this.o.version, run_id: this.o.runId, step: "summary",
      event_id: this.eventId("summary", 1), cost_role: "summary", recorded_at: this.now(),
      metadata: { terminal_state: terminalState, framework: "vercel-ai", ...extra },
    }, `${this.o.runId}:summary`);
  }

  outcome(accepted: boolean | null, maturity: string = "final", occurredAt?: string): void {
    this.post("/v1/outcomes", {
      workflow: this.o.workflow, workflow_version: this.o.version, run_id: this.o.runId, accepted, maturity,
      outcome_type: "accepted", occurred_at: occurredAt ?? this.now(), metadata: {},
    }, `${this.o.runId}:${maturity}`);
  }

  /** Resolve once every queued delivery has been acknowledged or given up. */
  async flush(): Promise<Report> {
    await this.queue;
    return this.report;
  }

  private post(path: string, body: Record<string, unknown>, id: string): void {
    this.queue = this.queue.then(() => this.deliver(path, body, id));
  }

  private async deliver(path: string, body: Record<string, unknown>, id: string): Promise<void> {
    let attempt = 0;
    while (true) {
      attempt += 1;
      this.report.attempts[id] = attempt;
      let reason: string;
      try {
        const response = await fetch(`${this.o.url}${path}`, { method: "POST", body: JSON.stringify(body),
          headers: { "content-type": "application/json", authorization: `Bearer ${this.o.key}` },
          signal: AbortSignal.timeout(this.o.timeoutMs) });
        if (response.ok) {
          const ack = (await response.json()) as { status: string };
          this.report.delivered += 1;
          if (ack.status === "duplicate") this.report.duplicates += 1;
          return;
        }
        reason = `http ${response.status}`;
        if (response.status < 500) { this.report.lost.push({ id, reason }); return; }
      } catch (error) {
        reason = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      }
      if (attempt > this.o.retries) { this.report.lost.push({ id, reason }); return; }
      await new Promise((resolve) => setTimeout(resolve, 50 * attempt));
    }
  }
}
