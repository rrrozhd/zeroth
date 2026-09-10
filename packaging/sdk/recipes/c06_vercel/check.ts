// Run the frozen Phase 3 reference workload and the C06 mode scenarios through the AI SDK app.
//
//   node check.ts --zeroth-url URL --zeroth-key KEY --workload workload.json [--version v1]
//                 [--scenario reference|modes] [--workflow NAME] [--retries N] [--timeout-ms MS]
import { parseArgs } from "node:util";
import { readFile } from "node:fs/promises";
import { Capture } from "./capture.ts";
import { Replies } from "./mocks.ts";
import * as app from "./app.ts";

type Call = { step: string; attempt: number; kind: string; model?: string; usage?: Record<string, [number, number, number] | null>; tool?: string; cost_usd?: Record<string, string> };
type RunSpec = { id: string; terminal: string; outcome: { accepted: boolean; delayed: boolean }; recorded_at: string; calls: Call[] };

const { values } = parseArgs({ options: {
  "zeroth-url": { type: "string" }, "zeroth-key": { type: "string" }, workload: { type: "string" },
  version: { type: "string", default: "v1" }, scenario: { type: "string", default: "reference" },
  workflow: { type: "string" }, retries: { type: "string", default: "3" }, "timeout-ms": { type: "string", default: "5000" } } });
const url = values["zeroth-url"]!.replace(/\/$/, "");
const key = values["zeroth-key"]!;
const workload = JSON.parse(await readFile(values.workload!, "utf8")) as { workflow: string; runs: RunSpec[] };
const workflow = values.workflow ?? workload.workflow;
const version = values.version!;
const delivery = { retries: Number(values.retries), timeoutMs: Number(values["timeout-ms"]) };

function capture(runId: string, recordedAt?: string): Capture {
  return new Capture({ url, key, workflow, version, runId, recordedAt, ...delivery });
}

function queued(replies: Replies, call: Call, text = "ok") {
  const tokens = call.usage![version];
  replies.expect(call.model!, { text, usage: tokens === null ? null : tokens });
}

/** One model call the way the recipe instruments it: callbacks charge steps, call() charges failures. */
function traced(cap: Capture, step: string, model: string, fn: (hooks: Record<string, unknown>) => Promise<unknown>) {
  return cap.call(step, model, () => fn(cap.callbacks(step, model)));
}

async function reference(): Promise<Capture[]> {
  const replies = new Replies();
  const captures: Capture[] = [];
  for (const run of workload.runs) {
    const cap = capture(run.id, run.recorded_at);
    captures.push(cap);
    const q = `question for ${run.id}`;
    const [c0, c1, c2] = run.calls;
    switch (run.id) {
      case "success":
        queued(replies, c0); queued(replies, c1);
        await traced(cap, "plan", c0.model!, (h) => app.plan(replies.model(c0.model!), q, h));
        await traced(cap, "answer", c1.model!, (h) => app.plan(replies.model(c1.model!), q, h));
        break;
      case "rejection": case "delayed_outcome": case "duplicate_delivery": case "missing_usage":
        queued(replies, c0);
        await traced(cap, "answer", c0.model!, (h) => app.plan(replies.model(c0.model!), q, h));
        break;
      case "retry":
        queued(replies, c0, "not json"); queued(replies, c1, '{"ok": true}');
        await app.robustExtract(replies.model(c0.model!), q, () => cap.callbacks("answer", c0.model!));
        break;
      case "parallel_calls":
        queued(replies, c0); queued(replies, c1); queued(replies, c2);
        await app.fanout(replies.model(c0.model!), [`${q} a`, `${q} b`], () => cap.callbacks("fanout", c0.model!));
        await traced(cap, "merge", c2.model!, (h) => app.plan(replies.model(c2.model!), "merge", h));
        break;
      case "tool_cost":
        cap.tool(c1.step, c1.tool!, c1.cost_usd![version]);
        queued(replies, c0);
        await traced(cap, "answer", c0.model!, (h) => app.plan(replies.model(c0.model!), q, h));
        break;
      case "fallback":
        replies.expect(c0.model!, { usage: null, error: "provider unavailable" });
        queued(replies, c1);
        try {
          await traced(cap, "answer", c0.model!, (h) => app.plan(replies.model(c0.model!), q, h));
        } catch {
          await traced(cap, "answer", c1.model!, (h) => app.plan(replies.model(c1.model!), q, h));
        }
        break;
      default:
        throw new Error(`unmapped family ${run.id}`);
    }
    cap.summary(run.terminal);
    let at = run.recorded_at;
    if (run.outcome.delayed) {
      cap.outcome(null, "provisional", at);
      at = new Date(Date.parse(at) + 3_600_000).toISOString();
    }
    cap.outcome(run.outcome.accepted, "final", at);
  }
  if (replies.pending) throw new Error(`${replies.pending} queued replies were not consumed`);
  return captures;
}

async function modes(): Promise<{ captures: Capture[]; report: Record<string, unknown> }> {
  const replies = new Replies();
  const captures: Capture[] = [];
  const report: Record<string, unknown> = {};
  const open = (id: string) => { const cap = capture(id); captures.push(cap); return cap; };

  let cap = open("modes-multistep");
  replies.expect(app.PLANNER, { toolCall: { name: "web_search", input: { query: "q" } }, usage: [20, 5] }, { text: "answer", usage: [60, 9, 10] });
  const multi = await cap.call("answer", app.PLANNER, () => app.answerWithTool(replies.model(app.PLANNER), "q",
    cap.callbacks("answer", app.PLANNER), () => cap.tool("web_search", "web_search", "0.00500000")));
  cap.summary("completed", { total_usage: multi.totalUsage });
  cap.outcome(true);
  report.multistep = { text: multi.text, steps: multi.steps };

  cap = open("modes-stream");
  replies.expect(app.QUICK, { text: "streamed", usage: [10, 2] });
  report.stream = await app.streamAnswer(replies.model(app.QUICK), "q", cap.callbacks("answer", app.QUICK));
  cap.summary("completed");

  cap = open("modes-stream-abort");
  replies.expect(app.QUICK, { text: "streamed", usage: [11, 2] });
  const controller = new AbortController();
  const hooks = cap.callbacks("answer", app.QUICK);
  try {
    await app.streamAnswer(replies.model(app.QUICK), "q", hooks, controller.signal, 1).then(() => controller.abort());
  } catch (error) { report.streamAbortError = (error as Error).name; }
  await new Promise((r) => setTimeout(r, 200));
  cap.summary("aborted");

  cap = open("modes-timeout");
  replies.expect(app.REVIEWER, { text: "late", usage: [1, 1], delayMs: 800 });
  try {
    await cap.call("answer", app.REVIEWER, () => app.plan(replies.model(app.REVIEWER), "q", cap.callbacks("answer", app.REVIEWER), AbortSignal.timeout(50)));
  } catch (error) { report.timeoutError = (error as Error).name; }
  cap.summary("failed");

  cap = open("modes-unknown");
  replies.expect("vendor/unlisted-model", { text: "?", usage: [10, 10] });
  await traced(cap, "answer", "vendor/unlisted-model", (h) => app.plan(replies.model("vendor/unlisted-model"), "q", h));
  cap.summary("completed");

  report.providerRequests = replies.requests.length;
  return { captures, report };
}

const started = performance.now();
const { captures, report } = values.scenario === "modes" ? await modes() : { captures: await reference(), report: {} };
const reports = await Promise.all(captures.map((c) => c.flush()));  // flush: every delivery acknowledged before exit
const lost = reports.flatMap((r) => r.lost);
console.log(JSON.stringify({ node: process.versions.node, ai: JSON.parse(await readFile(new URL("./node_modules/ai/package.json", import.meta.url), "utf8")).version,
  scenario: values.scenario, version, workflow, delivered: reports.reduce((n, r) => n + r.delivered, 0),
  duplicates: reports.reduce((n, r) => n + r.duplicates, 0), retried: reports.reduce((n, r) => n + Object.values(r.attempts).filter((a) => a > 1).length, 0),
  lost, elapsed_ms: Math.round(performance.now() - started), report }));
process.exitCode = lost.length ? 1 : 0;
