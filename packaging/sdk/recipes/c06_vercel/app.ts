// A representative Vercel AI SDK application. It does not import Zeroth: `hooks` are whatever
// callbacks the caller wants spread into the SDK calls, `onTool` is its own accounting hook.
import { generateText, streamText, tool, stepCountIs } from "ai";
import { z } from "zod";

export const PLANNER = "gpt-4.1-mini", REVIEWER = "gpt-4.1", WRITER = "claude-sonnet-4-20250514", QUICK = "claude-haiku-4-5-20251001";

type Hooks = Record<string, unknown>;

export async function plan(model: any, question: string, hooks: Hooks = {}, signal?: AbortSignal): Promise<string> {
  const result = await generateText({ model, prompt: question, abortSignal: signal, ...hooks });
  return result.text;
}

export async function answerWithTool(model: any, question: string, hooks: Hooks = {}, onTool?: (query: string) => void) {
  const result = await generateText({
    model, prompt: question, stopWhen: stepCountIs(3), ...hooks,
    tools: { web_search: tool({ description: "Search the web.", inputSchema: z.object({ query: z.string() }),
      execute: async ({ query }) => { onTool?.(query); return `results for ${query}`; } }) },
  });
  return { text: result.text, steps: result.steps.length, totalUsage: result.totalUsage };
}

export async function streamAnswer(model: any, question: string, hooks: Hooks = {}, signal?: AbortSignal, stopAfterChunks = Infinity): Promise<string> {
  const result = streamText({ model, prompt: question, abortSignal: signal, ...hooks });
  let text = "", chunks = 0;
  for await (const delta of result.textStream) {
    text += delta;
    if (++chunks >= stopAfterChunks) break;
  }
  return text;
}

export async function robustExtract(model: any, text: string, hooks: () => Hooks): Promise<unknown> {
  for (let attempt = 1; attempt <= 2; attempt++) {
    const reply = await plan(model, `Return JSON for: ${text}`, hooks());
    try { return JSON.parse(reply); } catch (error) { if (attempt === 2) throw error; }
  }
}

export async function fanout(model: any, questions: string[], hooks: () => Hooks): Promise<string[]> {
  return Promise.all(questions.map((q) => plan(model, q, hooks())));
}
