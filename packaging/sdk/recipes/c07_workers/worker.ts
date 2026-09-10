// C07 - explicit economic events from a TypeScript worker. Node built-ins only:
// run with `node worker.ts` (Node >= 23.6) or `node --experimental-strip-types worker.ts`
// (Node >= 22.6). No npm package, no Zeroth runtime, no Python.
//
//   node worker.ts --zeroth-url URL --zeroth-key KEY --workload workload.json --version v1
//   node worker.ts ... --compare v1 v2      # version comparison through the same HTTP contract
//
// Identity, metadata and pricing mirror zeroth.instrumentation (INTERFACE.md):
// event_id = workflow:version:run:step:attempt, charge_id = "charge:" + event_id,
// one charge per physical call, one money-free summary per run, outcomes are yours.
// Delivery is idempotent, so a retry of a lost acknowledgement is a "duplicate", never a
// second row. 5xx, network errors and timeouts are retried; 4xx are reported as lost.

import { parseArgs } from "node:util";

const RATE_CARD_VERSION = "2026-09-10/litellm-1.98.0";
// USD per million tokens, copied from zeroth.instrumentation.rate_card (parity-tested).
const RATES: Record<string, { provider: string; input: string; output: string; cacheRead: string; cacheWrite: string | null }> = {
  "gpt-4.1": { provider: "openai", input: "2.0", output: "8.0", cacheRead: "0.5", cacheWrite: null },
  "gpt-4.1-mini": { provider: "openai", input: "0.4", output: "1.6", cacheRead: "0.1", cacheWrite: null },
  "claude-sonnet-4-20250514": { provider: "anthropic", input: "3.0", output: "15.0", cacheRead: "0.3", cacheWrite: "3.75" },
  "claude-haiku-4-5-20251001": { provider: "anthropic", input: "1.0", output: "5.0", cacheRead: "0.1", cacheWrite: "1.25" },
};

type Usage = { input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number; reasoning_tokens: number };
type Call = { step: string; attempt: number; kind: "model" | "tool"; model?: string; provider?: string; error?: string | null; usage?: Record<string, number[] | null>; tool?: string; cost_usd?: Record<string, string> };
type RunSpec = { id: string; terminal: string; outcome: { accepted: boolean; delayed: boolean }; recorded_at: string; calls: Call[] };
type Workload = { workflow: string; runs: RunSpec[] };

// A rate like "3.75" per million tokens is 375 units of 1e-8 USD per token: exact integers only.
function unitsPerToken(rate: string): bigint {
  const [whole, fraction = ""] = rate.split(".");
  return BigInt(whole) * 100n + BigInt((fraction + "00").slice(0, 2));
}

function money(units: bigint): string {
  const whole = units / 100_000_000n;
  const fraction = (units % 100_000_000n).toString().padStart(8, "0");
  return `${whole}.${fraction}`;
}

function price(model: string, usage: Usage): string | null {
  const card = RATES[model];
  if (!card) return null;
  let units = BigInt(usage.input_tokens) * unitsPerToken(card.input) + BigInt(usage.output_tokens) * unitsPerToken(card.output);
  if (usage.cache_read_tokens) units += BigInt(usage.cache_read_tokens) * unitsPerToken(card.cacheRead);
  if (usage.cache_write_tokens) {
    if (card.cacheWrite === null) return null;
    units += BigInt(usage.cache_write_tokens) * unitsPerToken(card.cacheWrite);
  }
  return money(units);
}

function eventId(workload: string, version: string, run: string, step: string, attempt: number): string {
  return `${workload}:${version}:${run}:${step}:${attempt}`;
}

function charge(workload: string, version: string, run: RunSpec, call: Call): Record<string, unknown> {
  const id = eventId(workload, version, run.id, call.step, call.attempt);
  const base = { workflow: workload, workflow_version: version, run_id: run.id, step: call.step, attempt: call.attempt,
    event_id: id, cost_role: "charge", charge_id: `charge:${id}`, recorded_at: run.recorded_at, latency_ms: 0 };
  if (call.kind === "tool") {
    const cost = call.cost_usd![version];
    return { ...base, model_version: `tool:${call.tool}`, cost_usd: cost, cost_measurement: "measured",
      metadata: { charge_kind: "tool", tool: call.tool, pricing: "caller", error: null } };
  }
  const tokens = call.usage![version];
  const usage: Usage | null = tokens === null ? null
    : { input_tokens: tokens[0], output_tokens: tokens[1], cache_read_tokens: tokens[2] ?? 0, cache_write_tokens: 0, reasoning_tokens: 0 };
  const cost = usage === null ? null : price(call.model!, usage);
  const pricing = usage === null ? "missing_usage" : cost === null ? "unknown_model" : "rate_card";
  return { ...base, model_version: call.model, cost_usd: cost, cost_measurement: cost === null ? "unmeasured" : "estimated",
    metadata: { charge_kind: "model", provider: call.provider ?? RATES[call.model!]?.provider ?? "unknown",
      provider_request_id: null, usage, pricing, rate_card_version: pricing === "rate_card" ? RATE_CARD_VERSION : null,
      error: call.error ?? null } };
}

function summary(workload: string, version: string, run: RunSpec): Record<string, unknown> {
  return { workflow: workload, workflow_version: version, run_id: run.id, step: "summary",
    event_id: eventId(workload, version, run.id, "summary", 1), cost_role: "summary", recorded_at: run.recorded_at,
    metadata: { terminal_state: run.terminal } };
}

function outcome(workload: string, version: string, run: RunSpec, accepted: boolean | null, maturity: string, at: string) {
  return { workflow: workload, workflow_version: version, run_id: run.id, accepted, maturity, outcome_type: "accepted",
    occurred_at: at, metadata: {} };
}

type Delivery = { path: string; body: Record<string, unknown>; id: string };

function plan(workload: Workload, version: string): Delivery[] {
  const out: Delivery[] = [];
  for (const run of workload.runs) {
    for (const call of run.calls) out.push({ path: "/v1/executions", body: charge(workload.workflow, version, run, call), id: `${run.id}:${call.step}:${call.attempt}` });
    out.push({ path: "/v1/executions", body: summary(workload.workflow, version, run), id: `${run.id}:summary` });
    let at = run.recorded_at;
    if (run.outcome.delayed) {
      out.push({ path: "/v1/outcomes", body: outcome(workload.workflow, version, run, null, "provisional", at), id: `${run.id}:provisional` });
      at = new Date(Date.parse(at) + 3_600_000).toISOString();
    }
    out.push({ path: "/v1/outcomes", body: outcome(workload.workflow, version, run, run.outcome.accepted, "final", at), id: `${run.id}:final` });
  }
  return out;
}

type Report = { delivered: number; duplicates: number; lost: { id: string; reason: string }[]; attempts: Record<string, number> };

async function post(url: string, key: string, path: string, body: unknown, timeoutMs: number): Promise<Response> {
  return fetch(`${url}${path}`, { method: "POST", headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs) });
}

async function deliver(url: string, key: string, deliveries: Delivery[], retries: number, timeoutMs: number): Promise<Report> {
  const report: Report = { delivered: 0, duplicates: 0, lost: [], attempts: {} };
  for (const d of deliveries) {
    let attempt = 0;
    while (true) {
      attempt += 1;
      report.attempts[d.id] = attempt;
      let reason: string;
      try {
        const response = await post(url, key, d.path, d.body, timeoutMs);
        if (response.ok) {
          const ack = (await response.json()) as { status: string };
          report.delivered += 1;
          if (ack.status === "duplicate") report.duplicates += 1;
          break;
        }
        reason = `http ${response.status}`;
        if (response.status < 500) { report.lost.push({ id: d.id, reason }); break; }
      } catch (error) {
        reason = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      }
      if (attempt > retries) { report.lost.push({ id: d.id, reason }); break; }
      await new Promise((resolve) => setTimeout(resolve, 50 * attempt));
    }
  }
  return report;
}

async function compare(url: string, key: string, workflow: string, baseline: string, candidate: string): Promise<unknown> {
  for (const version of [baseline, candidate]) {
    const response = await post(url, key, "/v1/debugger/outcome-definitions",
      { workflow_id: workflow, workflow_version: version, outcome_type: "accepted", operator: "equals", target: true }, 10_000);
    if (!response.ok) throw new Error(`outcome definition ${version}: http ${response.status}`);
  }
  const response = await post(url, key, "/v1/decisions/compare", { workflow, baseline_version: baseline, candidate_version: candidate,
    policy: { min_runs: 4, min_success_rate: 0.5, allow_estimated_cost: true } }, 30_000);
  if (!response.ok) throw new Error(`compare: http ${response.status} ${await response.text()}`);
  return response.json();
}

async function main(): Promise<void> {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    "zeroth-url": { type: "string" }, "zeroth-key": { type: "string" }, workload: { type: "string" },
    version: { type: "string", default: "v1" }, retries: { type: "string", default: "3" },
    "timeout-ms": { type: "string", default: "5000" }, compare: { type: "boolean", default: false } } });
  const url = values["zeroth-url"]!.replace(/\/$/, "");
  const key = values["zeroth-key"]!;
  const { readFile } = await import("node:fs/promises");
  const workload = JSON.parse(await readFile(values.workload!, "utf8")) as Workload;
  if (values.compare) {
    console.log(JSON.stringify({ node: process.versions.node, decision: await compare(url, key, workload.workflow, positionals[0], positionals[1]) }));
    return;
  }
  const report = await deliver(url, key, plan(workload, values.version!), Number(values.retries), Number(values["timeout-ms"]));
  console.log(JSON.stringify({ node: process.versions.node, version: values.version, ...report }));
  process.exitCode = report.lost.length === 0 ? 0 : 1;
}

await main();
