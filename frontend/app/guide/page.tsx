"use client";

import Link from "next/link";
import { NODE_META, NodeGlyph } from "@/app/components/nodeMeta";
import { Card, Mono, PageHeader } from "@/app/components/ui";

// In-console guide: enough concept + how-to context that a new operator can go
// from an empty deployment to a governed run without leaving the console.

const CONCEPTS: { term: string; def: React.ReactNode }[] = [
  {
    term: "Workflow",
    def: (
      <>
        A graph of nodes (agents, tools, approval gates…) authored in{" "}
        <Link href="/studio" className="text-accent hover:underline">
          Studio
        </Link>
        . Drafts are editable; published versions are immutable.
      </>
    ),
  },
  {
    term: "Deployment",
    def: "A published graph served by this API as a standalone service. The console always operates on the deployment you're connected to.",
  },
  {
    term: "Run",
    def: (
      <>
        One execution of the deployed graph for an input payload — submitted from the{" "}
        <Link href="/runs" className="text-accent hover:underline">
          Runs
        </Link>{" "}
        page or via <Mono>POST /v1/runs</Mono>.
      </>
    ),
  },
  {
    term: "Governance",
    def: (
      <>
        Human-approval gates pause runs in{" "}
        <Link href="/approvals" className="text-accent hover:underline">
          Approvals
        </Link>
        , every node execution is recorded in{" "}
        <Link href="/audit" className="text-accent hover:underline">
          Audit
        </Link>
        , and provider spend is attributed in{" "}
        <Link href="/cost" className="text-accent hover:underline">
          Cost
        </Link>
        .
      </>
    ),
  },
];

const STEPS: { title: string; body: React.ReactNode }[] = [
  {
    title: "Connect",
    body: (
      <>
        Use <span className="font-medium text-foreground">Connect</span> in the top right to set
        the API base URL and your operator key (sent as <Mono>X-API-Key</Mono>). The Overview page
        shows the deployment you&apos;re connected to.
      </>
    ),
  },
  {
    title: "Create a workflow",
    body: (
      <>
        In{" "}
        <Link href="/studio" className="text-accent hover:underline">
          Studio
        </Link>
        , start from a template — each one is a small working graph you can reshape — or create a
        blank workflow and add nodes from the palette.
      </>
    ),
  },
  {
    title: "Configure the nodes",
    body: (
      <>
        Click a node to open its editor. Fields marked{" "}
        <span className="font-semibold text-red-600 dark:text-red-400">*</span> are required for
        the graph to publish; each field shows a hint with an example value.
      </>
    ),
  },
  {
    title: "Save, publish, deploy",
    body: (
      <>
        Saving keeps everything in the draft. Publishing and running additionally need contracts,
        a registered runner, and a deployment — the medium-code path done from your project code,
        where the graph is wired to real tools and models.
      </>
    ),
  },
  {
    title: "Submit a run",
    body: (
      <>
        From the{" "}
        <Link href="/runs" className="text-accent hover:underline">
          Runs
        </Link>{" "}
        page (it has example payloads) or from your application via the API — see the snippet
        below. The payload shape is defined by your graph&apos;s input contract.
      </>
    ),
  },
  {
    title: "Operate",
    body: (
      <>
        Watch the run&apos;s status and timeline, resolve any{" "}
        <Link href="/approvals" className="text-accent hover:underline">
          Approvals
        </Link>{" "}
        it pauses on, and inspect the per-node trail in{" "}
        <Link href="/audit" className="text-accent hover:underline">
          Audit
        </Link>{" "}
        — including tool calls, memory reads, tokens, and cost.
      </>
    ),
  },
];

// Example config per node type, mirroring the NodeInspector fields.
const NODE_EXAMPLES: Record<string, string> = {
  agent: '{ "instruction": "Summarize the input…", "model_provider": "openai/gpt-4o" }',
  executable_unit: '{ "manifest_ref": "tools/my_tool", "execution_mode": "native" }',
  human_approval: '{ "sla_timeout_seconds": 86400 }',
  retrieval: '{ "connector_ref": "docs", "top_k": 5 }',
  subgraph: '{ "graph_ref": "my-subflow", "version": 2 }',
};

const CURL_EXAMPLE = `curl -X POST "$API_BASE/v1/runs" \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: $ZEROTH_API_KEY" \\
  -d '{"input_payload": {"question": "What is Zeroth?"}}'`;

export default function GuidePage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Guide"
        subtitle="From an empty deployment to a governed run, without leaving the console."
      />

      <Card title="The big picture">
        <dl className="grid gap-x-8 gap-y-4 text-sm sm:grid-cols-2">
          {CONCEPTS.map((c) => (
            <div key={c.term}>
              <dt className="font-medium">{c.term}</dt>
              <dd className="mt-1 leading-relaxed text-muted">{c.def}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card title="From zero to a running workflow">
        <ol className="space-y-4">
          {STEPS.map((s, i) => (
            <li key={s.title} className="flex gap-3 text-sm">
              <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-accent/10 text-xs font-semibold text-accent">
                {i + 1}
              </span>
              <div>
                <div className="font-medium">{s.title}</div>
                <p className="mt-0.5 leading-relaxed text-muted">{s.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </Card>

      <Card title="Node types">
        <ul className="divide-y divide-border">
          {Object.entries(NODE_META).map(([type, meta]) => (
            <li key={type} className="flex gap-3 py-3 first:pt-0 last:pb-0">
              <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-md bg-accent/10 text-accent">
                <NodeGlyph type={type} className="h-4 w-4" />
              </span>
              <div className="min-w-0 text-sm">
                <div className="font-medium">
                  {meta.blurb}{" "}
                  <span className="font-mono text-xs font-normal text-muted">{type}</span>
                </div>
                <p className="mt-0.5 leading-relaxed text-muted">{meta.help}</p>
                {NODE_EXAMPLES[type] && (
                  <code className="mt-1.5 block overflow-x-auto whitespace-nowrap rounded bg-zinc-100 px-2 py-1 font-mono text-xs text-muted dark:bg-zinc-800">
                    {NODE_EXAMPLES[type]}
                  </code>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Call the deployment from your app">
        <p className="text-sm leading-relaxed text-muted">
          Anything the console does, your application can do — the console is a client of the same
          API. Submit a run with the key you connected with:
        </p>
        <pre className="mt-3 overflow-x-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-relaxed text-zinc-700 ring-1 ring-border dark:bg-zinc-900/60 dark:text-zinc-300">
          {CURL_EXAMPLE}
        </pre>
        <p className="mt-3 text-sm leading-relaxed text-muted">
          The response includes a <Mono>run_id</Mono> — poll <Mono>GET /v1/runs/{"{run_id}"}</Mono>{" "}
          for status and output. Pass a <Mono>thread_id</Mono> to group multi-turn runs into one
          conversation.
        </p>
      </Card>
    </div>
  );
}
