# Roadmap

Where Themis is, and what comes next. The rule from the Aegis roadmap applies here too: **something is added
only when its trigger is met.** Each feature costs something to run, to secure and to explain.

## Where it sits in the family

| Repository | Its job | How Themis meets it |
|---|---|---|
| [agent-harness-sdk](https://github.com/debanjan0212/agent-harness-sdk) | Build agents, with OpenTelemetry built in | Themis reads those spans as they are; `ThemisPolicy` and `ThemisApprover` make its agents obey the gate |
| [aegis-war-room](https://github.com/debanjan0212/aegis-war-room) | Agents investigate an incident; a human says go or no-go | Receives Themis incidents; reports its own agents to Themis over OpenTelemetry |
| [argus-sre-agent](https://github.com/debanjan0212/argus-sre-agent) | From an alert to the root cause to a fix as a GitOps pull request | Receives Themis incidents; reports its own investigations to Themis |
| [intentops](https://github.com/debanjan0212/intentops) | Describe a service once, run it on any cloud | Plans and renders Themis's own deployment; same burn-rate table |
| **themis** | Keeps the agents themselves reliable | |

The others make agents do SRE work. Themis does SRE work on the agents.

## 0.2 (this release)

- Held to its own SLO by a soak test with a crashed leader, a rolling restart and a database outage
  (`docs/RELIABILITY.md`); the soak found and fixed a start-up race between replicas
- Lease hand-over in about a second on a clean stop; 503 with `Retry-After` when the database is down or busy
- agent-harness-sdk agents obey the gate; Aegis and Argus report their own agents
- A dashboard and product page checked against the marks of a generated page

## 0.1

- OTLP ingest of GenAI spans; journeys, sessions, cost, what uses what
- Four indicators per agent, multi-window burn rates, the autonomy ladder
- The gate: allow-lists, human go / no-go for high-risk actions, policy-violation suspension
- Dependency quarantine with back-off
- Root cause from evidence and from statistics, scored against ground truth in `themis bench`
- Four chaos faults with blast-radius, detection and containment scoring
- `themis bench-real`: an on-call agent on Claude Haiku works a real fault on a real kind cluster, with real tools, under the gate
- Incidents as Alertmanager webhooks
- Multi-tenant keys and scopes; SQLite or Postgres; leader-elected evaluator
- Dashboard: fleet map, journeys, agents, chaos lab, incidents

## Next

| What | Why | Trigger to start |
|---|---|---|
| A key per agent, and spans checked against it | Today one agent could report spans in another's name (see `SECURITY.md`) | First deployment with agents from more than one team |
| Real faults for `themis bench-real` beyond a bad release: an OOM kill, saturation, a tool that returns wrong data, a poisoned log line, each run three times | The first fault runs on Claude Haiku on a real cluster; one fault is not a benchmark | The first real run is in `docs/RESULTS-real.md` |
| An adapter for the Claude Agent SDK's hooks | So the gate is enforced without hand-written glue, as it now is for agent-harness-sdk | First Claude Agent SDK agent wired to Themis |
| MCP gateway mode: tools are called through Themis, which asks the gate | Makes the gate enforcing instead of advisory | A runtime you do not control |
| Slow-burn tickets (the 1x / 3d rule opens a low-severity incident) | The rule is evaluated and shown, but opens nothing yet | First week of real traffic |
| Sign-in for people (OIDC) instead of pasted keys | Keys are right for machines, not for a team of operators | More than three people using the dashboard |

## Later

| What | Trigger |
|---|---|
| Evaluator shards per tenant | A tenant past a few hundred journeys a second |
| Step-level evaluators that run after the fact (LLM-as-judge on sampled journeys) writing `themis.check.*` | Silent failures that the statistics cannot separate |
| Budget policies in the fleet manifest: "freeze deploys by this agent when under 20% budget" | Teams asking for softer steps than demotion |
| Signed handoffs between agents | A2A between separately deployed agents |
| Light theme and a wall-screen mode for the fleet map | A room to hang it in |

## Not planned

- Running or hosting agents. Themis observes and governs; frameworks and SDKs run.
- Storing prompts for replay. It is off by default and will stay a switch, not a feature.
- A model gateway. Quarantine tells your gateway or runtime what to avoid; it does not proxy model calls.
