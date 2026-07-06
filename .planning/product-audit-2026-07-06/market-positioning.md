# Zeroth — Market Positioning Analysis (mid-2026)

Research date: 2026-07-06. Scope: competitive landscape for a governed medium-code
platform for building/running/deploying multi-agent systems as API services.
Evidence: 11 web searches; all competitor claims cited inline.

---

## 1. Competitive landscape map

### 1a. Orchestration-first platforms (the incumbents Zeroth is measured against)

**LangGraph / LangSmith (LangChain)** — the reference competitor.
- LangGraph Platform was renamed **LangSmith Deployment** (Oct 2025); LangGraph is the OSS
  framework, LangSmith is the commercial plane (tracing, evals, deployment).
- Pricing: Developer free (5K traces); Plus **$39/seat/mo** with 10K traces; Enterprise custom
  with self-hosting. Deployment runs **$0.005/run**; uptime billed $0.0007–0.0036/min;
  trace overage $2.50–5.00 per 1K. ([langchain.com/pricing](https://www.langchain.com/pricing),
  [zenml.io/blog/langgraph-pricing](https://www.zenml.io/blog/langgraph-pricing))
- Positioning: "agent engineering platform" — build (LangGraph) + observe/eval (LangSmith)
  + deploy (LangSmith Deployment). Has HITL primitives in-framework
  ([docs.langchain.com HITL](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)).
- Governance is observational (traces, evals), not enforced (no capability enforcement,
  no tenant budget caps that halt runs, no policy bindings).

**CrewAI** — the enterprise-motion leader among frameworks.
- Free (50 executions/mo), Pro **$25/mo**, Enterprise estimated **$60K–120K/yr**.
  Claims 150+ enterprise customers, 2B agent executions/12mo, "used by 63% of Fortune 500".
  ([techjacksolutions.com CrewAI pricing](https://techjacksolutions.com/ai-tools/crewai/crewai-pricing/),
  [zenml.io/blog/crewai-pricing](https://www.zenml.io/blog/crewai-pricing))
- Enterprise = SOC2/HIPAA, SSO, PII masking, on-prem ("CrewAI Factory" containers) or
  managed SaaS ("CrewAI AMP"), Forward Deployed Engineers.
- Positioning: workflow-operations / business-automation story, not compliance-grade audit.

**OpenAI AgentKit / Agents SDK** — gravity well, but platform churn.
- AgentKit launched with visual Agent Builder + Evals + Connector Registry
  ([openai.com/index/introducing-agentkit](https://openai.com/index/introducing-agentkit/)).
- **Critical signal: OpenAI is winding down Agent Builder and Evals — gone from the platform
  Nov 30, 2026**; recommends code-first Agents SDK or ChatGPT Workspace Agents instead
  ([developers.openai.com agents guide](https://developers.openai.com/api/docs/guides/agents),
  [techcrunch.com Apr 2026](https://techcrunch.com/2026/04/15/openai-updates-its-agents-sdk-to-help-enterprises-build-safer-more-capable-agents/)).
- Validates the "medium-code" thesis: pure no-code canvas from a model vendor failed;
  code-first SDK + optional console is where the market settled.

**Anthropic Claude Agent SDK + Managed Agents.**
- Renamed from Claude Code SDK; in 2026 "the default way developers wire Claude into
  long-running tool-using workflows" (search demand 50→14,800/mo May'25→Apr'26).
  Managed Agents adds hosting/scheduling on top.
  ([morphllm.com framework comparison](https://www.morphllm.com/ai-agent-framework),
  [code.claude.com Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview))
- Single-vendor, no multi-tenant governance plane, no cost attribution product. A build
  substrate, not a governed platform — complementary more than competitive.

**AutoGen / AG2** — classic AutoGen in maintenance mode; AG2 is the community successor
(event-driven, async). Framework-only, no commercial governance/deployment plane.
([pypi.org/project/autogen](https://pypi.org/project/autogen/),
[qubittool.com 2026 showdown](https://qubittool.com/blog/ai-agent-framework-comparison-2026))
2026 consolidation: six frameworks dominate — Claude Agent SDK, AWS Strands, LangGraph,
OpenAI Agents SDK, CrewAI, AG2.

**Temporal** — durable-execution infrastructure adopted *under* agent stacks (integrations
with OpenAI Agents SDK and Google ADK announced at Replay 2026; Mistral "Workflows" is
Temporal-powered). Infrastructure layer, zero governance/econ semantics — a component
Zeroth could embed, not a positioning rival.
([temporal.io/solutions/ai](https://temporal.io/solutions/ai),
[olmecdynamics.com](https://olmecdynamics.com/news/temporal-durable-execution-agentic-workflows-2026))

### 1b. Low/no-code builders

**n8n** — Starter €24/mo → Business €800/mo → Enterprise custom; per-execution billing;
AI agent nodes free at platform level. Massive SMB automation reach; governance is
enterprise-checkbox thin (secret store, log streaming), no per-node audit or budget
enforcement. ([n8n.io/pricing](https://n8n.io/pricing/),
[lowcode.agency n8n pricing](https://www.lowcode.agency/blog/n8n-pricing))

**Dify** — production-grade LLM app builder; Pro ~$59/mo; **enterprise via AWS Marketplace
from ~$150K/yr**; real multi-workspace RBAC. **Flowise** — Apache-2.0 experimentation tool,
"not a governed platform" (no roles/workspaces in core).
([dify.ai/pricing](https://dify.ai/pricing),
[use-apify.com Dify vs Flowise](https://use-apify.com/blog/dify-vs-flowise-2026),
[elest.io comparison](https://blog.elest.io/dify-vs-langflow-vs-flowise-which-open-source-llm-app-builder-actually-ships-to-production/))

### 1c. LLMOps / evals platforms

**Vellum** — free → Pro **$500/mo** → Enterprise custom; SOC2 Type II + HIPAA + BAA + VPC;
token pass-through at cost; $20M Series A Jul 2025. Positioning: consolidated
build/test/deploy/monitor LLMOps. ([vellum.ai/pricing](https://www.vellum.ai/pricing),
[zenml.io Vellum pricing](https://www.zenml.io/blog/vellum-ai-pricing))
**Orq.ai** — similar collaborative LLMOps for enterprise teams.
**Braintrust** — commercial evals/observability, "speed and ease of use" for mixed-technical
teams. ([braintrust.dev buyers guide](https://www.braintrust.dev/articles/best-ai-observability-tools-2026))

### 1d. Agent observability & tracing standards

- Langfuse (MIT-core, self-hostable), Arize (+ OSS Phoenix), MLflow, Laminar, Confident AI.
- **OpenTelemetry GenAI conventions heading to stable; OpenInference (Arize) instruments
  40+ frameworks and dual-emits OTel attributes** — "instrument once, route anywhere" is
  becoming the buyer expectation.
  ([aimultiple.com](https://aimultiple.com/agentic-monitoring),
  [digitalapplied.com observability stack guide](https://www.digitalapplied.com/blog/ai-agent-observability-2026-tracing-monitoring-stack-guide))
- Market sizing: LLM observability ~$1.97B (2025) → ~$2.69B (2026); only ~15% of GenAI
  deployments instrument observability today (Gartner via trade press).

### 1e. AI governance platforms (the category Zeroth's thesis collides with)

- **Credo AI** — created the "AI governance" category, policy/compliance layer, Fast Company
  Most Innovative 2026. ([credo.ai](https://www.credo.ai/))
- **Arthur** — first "Agent Discovery & Governance (ADG)" platform, framework-neutral.
  **Fiddler** — audit-grade observability for regulated industries.
  ([arthur.ai](https://www.arthur.ai/column/best-ai-governance-platforms-2026),
  [superblocks.com 9 best](https://www.superblocks.com/blog/ai-agent-governance-platform))
- **ServiceNow AI Control Tower** — expanded at Knowledge 2026 to "discover, observe, govern,
  secure, and measure AI deployed across any system"; **acquired Traceloop** for runtime
  agent observability; governance demand is materially driving NOW's enterprise momentum.
  ([servicenow newsroom](https://newsroom.servicenow.com/press-releases/details/2026/ServiceNow-expands-AI-Control-Tower-to-discover-observe-govern-secure-and-measure-AI-deployed-across-any-system-in-the-enterprise/default.aspx),
  [ciodive.com](https://www.ciodive.com/news/servicenow-earnings-ai-governance-control-tower/804438/))
- **Salesforce Agentforce** — **$1B+ ARR** (FY27 Q1, triple-digit YoY), adding observability
  controls. ([finance.yahoo.com](https://finance.yahoo.com/technology/ai/articles/strategic-partnerships-help-now-scale-140100652.html))
- IBM watsonx.governance, UiPath — compliance layers atop existing estates.
- MCP-gateway entrants: Runlayer, MintMCP; AI-control-plane: Speakeasy, TrueFoundry.
- Gartner: >40% of agentic AI projects canceled by end-2027 on cost/value/risk grounds;
  AI-governance platform spend >$1B by 2030; 72% of enterprises in production with agents
  but a "60% governance gap".
  ([agenticaiinstitute.org](https://agenticaiinstitute.org/agentic-ai-enterprise-adoption-2026-governance-gap/))

### 1f. Cost control / econ plane

- **Portkey** — AI gateway with virtual keys, **spend caps/budget limits, RBAC, 50+
  guardrails**; $49/mo Production tier; **Palo Alto Networks announced intent to acquire
  (Apr 30, 2026)** — security incumbents are buying the governance-gateway layer.
  ([portkey.ai/pricing](https://portkey.ai/pricing),
  [portkey budget-limits docs](https://portkey.ai/docs/product/ai-gateway/virtual-keys/budget-limits),
  [truefoundry.com Portkey guide](https://www.truefoundry.com/blog/portkey-pricing-guide))
- FinOps-for-AI tools (Amnic, Finout, FinOps LLM, Vantage-class): team/cost-center token
  attribution, chargeback, anomaly detection — 98% of FinOps teams now manage AI spend
  (up from 31% two years ago). ([amnic.com](https://amnic.com/blogs/finops-tools-for-ai-cost-management),
  [finout.io](https://www.finout.io/blog/best-finops-tools-for-managing-ai-costs-in-2026),
  [finops.org](https://www.finops.org/wg/finops-for-ai-overview/))
- Gap: gateways attribute at the *model-call* level, FinOps tools at the *invoice* level.
  Nobody attributes at the *agent-graph node* level with in-runtime enforcement.

### 1g. Human-in-the-loop

HITL approval is commoditizing as a *framework feature* (LangChain, OpenAI SDK, Cloudflare
Agents, Microsoft Agent Framework all ship interrupt/approve primitives); HumanLayer is the
standalone API play (Slack/email approval channels). No one packages approvals + audit +
policy as one governed runtime surface.
([openai HITL](https://openai.github.io/openai-agents-python/human_in_the_loop/),
[cloudflare HITL](https://developers.cloudflare.com/agents/concepts/human-in-the-loop/))

---

## 2. Whitespace: is "governance + audit + cost control" owned?

**Verdict: the thesis holds, with a precise framing — the whitespace is *enforcement in the
runtime*, not governance as a category.** The market has split into two camps that don't
meet:

1. **Orchestration-first platforms** (LangSmith, CrewAI, AgentKit, Dify) — they *run* agents
   and bolt on observability/enterprise checkboxes. Governance is passive: traces you can
   read after the fact, not policies enforced mid-run. None do per-node capability
   enforcement, tenant budget caps that halt execution, or typed/redacted audit records.
2. **Governance-overlay platforms** (Credo, Arthur, Fiddler, ServiceNow Control Tower,
   watsonx.governance, Portkey-style gateways) — they *observe and govern* agents built
   elsewhere. They can't enforce inside the agent graph because they don't own the runtime;
   the gateway camp enforces only at the model-call boundary.

Zeroth's architecture — policies, capability enforcement, typed per-node audit with
redaction, approval gates, and budget enforcement *inside* the orchestrator that also
deploys the service — is the only configuration where governance is a property of
execution rather than a report about it. No surveyed player owns that.

**Demand catalyst with a date: EU AI Act Annex III obligations became enforceable
August 2, 2026** — next month. Article 12 mandates event-level logging (inputs, outputs,
parameters, timestamps; 6-month retention), Article 14 human oversight, Article 26 deployer
duties; agent chains extend the compliance boundary to *every agent performing a high-risk
function*, including tool/MCP invocations. Penalties to €15M or 3% of turnover.
([artificialintelligenceact.eu Art. 12/16/26](https://artificialintelligenceact.eu/article/26/),
[helpnetsecurity.com agent logging](https://www.helpnetsecurity.com/2026/04/16/eu-ai-act-logging-requirements/),
[legalnodes.com](https://www.legalnodes.com/article/eu-ai-act-2026-updates-compliance-requirements-and-business-risks))
Zeroth's typed node-audit records + approval gates map almost one-to-one onto Art. 12/14 —
none of the orchestration platforms market that mapping today.

**But the window is short (12–24 months).** Convergence signals: ServiceNow bought
Traceloop; Palo Alto is buying Portkey; LangSmith keeps absorbing adjacent layers;
CrewAI's enterprise tier already sells SOC2/HIPAA/PII-masking. "Governed runtime" must be
claimed before an orchestration incumbent credibly bolts it on.

---

## 3. Recommendation

### Primary ICP
- **Buyer:** VP Engineering / Head of Platform at a 200–5,000-person company in a regulated
  or EU-exposed vertical (fintech, insurance, healthtech, HR-tech, B2B SaaS selling into
  EU enterprises). Economic influencers: CISO / compliance officer (AI Act exposure) and
  CFO/FinOps (LLM spend).
- **User:** platform / ML engineers building internal and customer-facing agents who have
  outgrown a raw framework but reject no-code lock-in — exactly the "medium-code" band.

### Positioning statement
> **Zeroth is the governed agent runtime: build multi-agent graphs in code or on canvas and
> deploy them as multi-tenant API services where audit trails, human approval gates, and
> per-tenant budget caps are enforced by the runtime itself — not bolted on afterward.**

### Candidate wedges, ranked
1. **Compliance-grade agent runtime for regulated / EU-exposed teams.** Only wedge with a
   dated forcing function (AI Act Annex III, Aug 2, 2026) and no incumbent owner: overlays
   can't enforce, orchestrators don't audit at node granularity. Sales artifact: "Article 12
   logging + Article 14 oversight, out of the box."
2. **Agent cost-control plane (runtime FinOps).** Per-node/per-run/per-tenant attribution
   with *enforced* caps is unowned (gateways stop at the model call, FinOps tools at the
   invoice); Gartner's 40%-of-projects-canceled-on-cost forecast makes this a CFO story.
   Strong as the #2 message inside wedge 1, weaker standalone (Portkey/Palo Alto adjacency).
3. **Internal agent platform-in-a-box.** Multi-tenant service layer + console + deploy-as-API
   is a real platform-team pain, but it's the most crowded frame (Dify, n8n, CrewAI AMP all
   pitch it) and dilutes the governance differentiator. Use as expansion story, not entry.

### Pricing sketch (anchored to observed comps)
- **Open-core:** runtime + graph + basic console free/self-hosted (credibility vs LangGraph/
  AG2/Flowise; table stakes for the medium-code buyer).
- **Team (hosted or licensed): ~$500–1,000/mo** — Vellum-Pro anchor; N production graph
  deployments, 30-day audit retention, cost dashboards, per-run metering (LangSmith
  precedent: ~$0.005/run) rather than per-seat.
- **Enterprise: $80K–150K/yr** — CrewAI ($60–120K) and Dify ($150K) bracket this; adds
  SSO/RBAC, VPC/on-prem, long-horizon audit retention (AI Act's 6–24 months), compliance
  pack (Art. 12 log export, oversight attestations), SLA.
- Never mark up tokens (Vellum's "pass-through at cost" is now the norm).

### GTM motion
**Design-partner-led open-core.** (a) 5–10 design partners in EU-regulated verticals signed
against the Aug 2026 deadline, converting to enterprise contracts; (b) OSS core for
bottoms-up developer trust; (c) content wedge: "EU AI Act readiness for agent systems" —
the compliance-mapping whitepaper nobody in the orchestration camp has written; (d) do NOT
fight LangChain/CrewAI for greenfield hobbyist devs — enter where a compliance or budget
mandate already exists.

---

## 4. Gap analysis — table stakes Zeroth lacks (per the stated feature list)

Ranked by how often they'll block a deal:

1. **Evals / testing harness.** LangSmith, Braintrust, Vellum, Confident AI all ship eval
   suites; "production-grade" claims without regression evals won't survive a bake-off.
   Minimum: dataset-based graph-level evals + LLM-judge scoring wired to the audit records
   Zeroth already emits.
2. **OpenTelemetry / OpenInference-standard trace export.** OTel GenAI conventions are
   nearing stable; buyers demand "instrument once, route to Datadog/Arize/Langfuse."
   Zeroth's typed audit records are a proprietary format — add an OTel exporter or the
   observability incumbents frame Zeroth as a silo.
3. **Streaming UX.** Token/event streaming (SSE/WebSocket) from deployed graph APIs and in
   the console run view — every surveyed framework has it; its absence reads as prototype.
4. **Prompt & graph versioning with rollback/diff.** Core LLMOps expectation (Vellum,
   Langfuse, LangSmith); also *strengthens* the compliance story ("what version made this
   decision" is an audit question).
5. **Own compliance certifications + enterprise auth.** SOC2 Type II (+ HIPAA path), SSO/
   SAML, RBAC — CrewAI, Vellum, Portkey all lead with these; a governance product without
   SOC2 is self-refuting in procurement.
6. Secondary gaps: TypeScript SDK / SDK ergonomics beyond Python; template gallery /
   marketplace (CrewAI templates, n8n's workflow library, LangGraph templates); model-gateway
   resilience (fallbacks, retries, caching — Portkey-class); durable-execution guarantees for
   long-running runs (Temporal-class replay/recovery, or embed Temporal).

## 5. Top competitive threats

1. **LangChain/LangSmith** — closest feature superset (build+observe+deploy+HITL), aggressive
   pricing ($39/seat), fastest to bolt on "governance mode" if the category gets hot.
2. **Incumbent absorption of the governance layer** — ServiceNow AI Control Tower (+Traceloop)
   and Palo Alto (+Portkey) selling "govern all agents everywhere" to the same CISO/compliance
   buyer Zeroth targets; they own procurement relationships even if they can't enforce in-runtime.
3. **CrewAI Enterprise** — already sells SOC2/HIPAA/PII-masking/on-prem at $60–120K to
   Fortune 500 workflow buyers; if it adds real audit/budget enforcement it collapses wedge 1
   from the orchestration side.
