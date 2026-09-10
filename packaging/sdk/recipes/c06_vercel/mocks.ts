// Deterministic language models for the checks: queued replies with explicit usage, no network.
import { MockLanguageModelV3, simulateReadableStream } from "ai/test";

export type Item = { text?: string; usage: [number, number, number?] | null; toolCall?: { name: string; input: Record<string, unknown> }; error?: string; delayMs?: number };

function usageV3(item: Item) {
  if (item.usage === null) return { inputTokens: { total: undefined, noCache: undefined, cacheRead: undefined, cacheWrite: undefined }, outputTokens: { total: undefined, text: undefined, reasoning: undefined } };
  const [input, output, cache = 0] = item.usage;
  return { inputTokens: { total: input + cache, noCache: input, cacheRead: cache, cacheWrite: 0 }, outputTokens: { total: output, text: output, reasoning: 0 } };
}

let ids = 0;

export class Replies {
  readonly queues = new Map<string, Item[]>();
  readonly requests: string[] = [];

  expect(model: string, ...items: Item[]): void {
    this.queues.set(model, [...(this.queues.get(model) ?? []), ...items]);
  }

  get pending(): number {
    return [...this.queues.values()].reduce((n, q) => n + q.length, 0);
  }

  private next(model: string, signal?: AbortSignal): Promise<Item> {
    const item = this.queues.get(model)?.shift();
    if (!item) throw new Error(`no queued reply for ${model}`);
    this.requests.push(model);
    return (async () => {
      if (item.delayMs) {
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(resolve, item.delayMs);
          signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason ?? new DOMException("aborted", "AbortError")); });
        });
      }
      if (item.error) throw new Error(item.error);
      return item;
    })();
  }

  model(name: string): MockLanguageModelV3 {
    return new MockLanguageModelV3({
      modelId: name,
      doGenerate: async (options: any) => {
        const item = await this.next(name, options.abortSignal);
        const content = item.toolCall
          ? [{ type: "tool-call", toolCallId: `call_${++ids}`, toolName: item.toolCall.name, input: JSON.stringify(item.toolCall.input) }]
          : [{ type: "text", text: item.text ?? "ok" }];
        return { content, finishReason: { unified: item.toolCall ? "tool-calls" : "stop" }, usage: usageV3(item), warnings: [],
          response: { id: `resp_${++ids}`, modelId: name } };
      },
      doStream: async (options: any) => {
        const item = await this.next(name, options.abortSignal);
        const text = item.text ?? "ok";
        return { stream: simulateReadableStream({ chunks: [
          { type: "stream-start", warnings: [] },
          { type: "response-metadata", id: `resp_${++ids}`, modelId: name },
          { type: "text-start", id: "t" }, { type: "text-delta", id: "t", delta: text.slice(0, 2) },
          { type: "text-delta", id: "t", delta: text.slice(2) }, { type: "text-end", id: "t" },
          { type: "finish", finishReason: { unified: "stop" }, usage: usageV3(item) },
        ], chunkDelayInMs: 15 }) };
      },
    });
  }
}
