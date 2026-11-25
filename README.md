# Themis — SRE for agent fleets

![ci](https://github.com/debanjan0212/themis-agent-sre/actions/workflows/ci.yml/badge.svg)
![security](https://github.com/debanjan0212/themis-agent-sre/actions/workflows/security.yml/badge.svg)

**Every agent gets an error budget. An agent that spends it too fast loses autonomy: first a human approves its risky actions, then it is stopped. Themis also tells you which agent, tool or model broke a user's request, and it proves all of this by breaking things on purpose.**
It runs with no cloud, no API key and no Docker. Any agent that already emits OpenTelemetry works with two environment variables.

![The fleet map while three faults are being handled](docs/images/fleet.png)

The human is at the centre. The less an agent is trusted, the closer it is pulled in.

```text
$ themis bench
warm-up  30s of healthy traffic
chaos   model_rate_limit on model:sim-large at 80% for 40s
        response: dependency quarantined, callers on fallback after 4.8s
        every agent is back within its budget
chaos   tool_wrong_result on tool:kb.search at 60% for 40s
        response: dependency quarantined, callers on fallback after 17.79s
        every agent is back within its budget
chaos   context_poison on agent:coder at 60% for 40s
        response: agent moved to suspended after 3.78s
        every agent is back within its budget
chaos   rogue_agent on agent:deployer at 30% for 40s
        response: agent moved to supervised after 3.06s
        every agent is back within its budget
ingest   60,000 spans from 4 clients
```

## What it does

- **Error-budget autonomy.** Four indicators per agent (tasks done, clean handoffs, safe actions, within cost),
  judged by the same 14.4x / 1h / 5m burn-rate rule used for services. Burn the budget and the agent moves from
  *autonomous* to *supervised* to *suspended*. Only a person un-suspends an agent.
- **Chaos for agents.** Five faults you can inject: a model answering 429, a tool that is silently wrong, a
  poisoned context, a rogue agent, a slow dependency. Each experiment is scored: how fast Themis reacted, how
  far the damage spread, and whether it named the right cause.
- **Cross-agent root cause.** When a journey fails, the cause is the earliest span with evidence (an
  error, a failed check, a denied action, a bad handoff). When nothing errored and the answer was simply wrong,
  statistics over recent journeys find what the failures have in common.
- **Journeys, like RUM for agents.** One user request followed across every agent, model, tool and pod it
  touched, with time, tokens and cost.

Tools and models that keep causing failures are **quarantined** so callers fall back, and every demotion or
quarantine goes out as an **incident** to whatever reads Alertmanager webhooks.

## Start here

This section is written for anyone. Go step by step and stop at the step you need.

### What you need

| What you want to do | What you need |
|---|---|
| **Try it** with the built-in ten-agent world | Python 3.11 or newer, Git |
| **Watch your own agents** | agents that emit OpenTelemetry GenAI spans (agent-harness-sdk does already) |
| **Run it for a team** | Postgres |
| **Run it highly available** | Kubernetes and Postgres. The manifests are in `deploy/kubernetes` |
| **Send incidents somewhere** | anything that accepts Alertmanager webhooks: Aegis War Room, Argus, Alertmanager tooling |

On Windows, use WSL2.

### Step 1: try it (2 minutes, nothing else needed)

```bash
git clone https://github.com/debanjan0212/themis-agent-sre.git
cd themis-agent-sre
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

themis demo
```

It prints a link. Open it. You are looking at ten simulated agents (a concierge, a planner, a coder, a
reviewer, a deployer and so on) serving simulated users. The models and tools are simulated. The spans are
not: they travel through the OpenTelemetry SDK and OTLP/HTTP into the same control plane you would deploy.

- **Fleet** is the live map. **Journeys** is every user request. Click one to see what it used.
- **Agents** shows each agent's targets and how much error budget is left.

### Step 2: break it on purpose

```bash
themis demo --chaos
```

After 25 healthy seconds it injects four faults, one after another, and prints what Themis did about each.
Keep the Fleet page open and watch: the deployer is pulled to the inner ring, the coder docks at the centre,
the model on the outer ring turns red. Time is compressed 120x in the demo, so a one-hour window passes in 30 seconds.

Or pick your own from the **Chaos lab** page, or the terminal:

```bash
export THEMIS_KEY=<the key from the demo link, the part after key=>
themis chaos rogue_agent agent:deployer --intensity 0.3 --duration 40
```

### Step 3: watch your own agents

```bash
themis serve                        # terminal 1: Themis on http://localhost:8600, data in ./themis.db
themis key --scopes admin           # terminal 2: a key for you. Paste it into the dashboard
themis key --scopes ingest          # and a key for your agents. Each key is printed once

export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8600
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20<the ingest key>"
# now run your agent as usual, in the same terminal
```

With [agent-harness-sdk](https://github.com/debanjan0212/agent-harness-sdk) that is everything. For other
frameworks, [docs/TELEMETRY.md](docs/TELEMETRY.md) lists the span attributes Themis reads. Prompts and tool
payloads are dropped on arrival unless you turn storage on.

### Step 4: let Themis control them

Seeing is half of it. To make demotion mean something, your agent runtime asks the gate before it acts. With
agent-harness-sdk that is two arguments:

```python
from agent_harness_sdk import Agent, ThemisApprover, ThemisGate, ThemisPolicy

gate = ThemisGate("http://localhost:8600", ingest_key).start()
agent = Agent("deployer", model=..., tools=[...], policy=ThemisPolicy(gate=gate), approver=ThemisApprover(gate))
```

Any other runtime uses the client directly:

```python
from themis.client import Gate

gate = Gate("http://localhost:8600", ingest_key)
gate.start()
gate.autonomy("deployer")  # "autonomous", "supervised" or "suspended"
gate.allowed("deployer", "tools", "deploy.apply")  # is it on the agent's allow-list?
await gate.approve("deployer", "tool:deploy.apply", {"change": "..."})  # waits for a human; no answer is a no
```

The full pattern is in [docs/INTEGRATION.md](docs/INTEGRATION.md). If Themis is unreachable the gate keeps its
last answer, so your agents do not stop because the control plane did.

### Step 5: send incidents to people

```bash
export THEMIS_WEBHOOKS="http://localhost:8000/api/alerts"    # Aegis War Room, for example
export THEMIS_WEBHOOK_ALLOW_PRIVATE=1    # needed for localhost and in-cluster addresses; off by default
themis serve
```

A suspended agent then opens a war-room incident in [Aegis](https://github.com/debanjan0212/aegis-war-room),
or starts an investigation in [Argus](https://github.com/debanjan0212/argus-sre-agent).

### Step 6: run it for a team

```bash
pip install -e ".[postgres]"
export THEMIS_DB_URL=postgresql+psycopg://themis@db.internal/themis
themis key --scopes admin           # your first key
themis serve --host 0.0.0.0
```

Run as many replicas as you like against the same Postgres. They all take traffic; one of them leads the
evaluator and another takes over if it dies. For Kubernetes, `deploy/kubernetes` has the manifests: 3 to 8
replicas across zones, a disruption budget, an autoscaler, a canary rollout judged against Themis's own SLO, and
a network policy. They are rendered by [IntentOps](https://github.com/debanjan0212/intentops) from
`deploy/intent.yaml`; change that file and run `make render`. Every setting is in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) and day-two operations are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## What was measured

`themis bench` runs the four faults under load and writes [docs/RESULTS.md](docs/RESULTS.md) from what it
measured. From the run in this repository (Themis 0.2.0, 3,955 journeys from 16 simulated users, on PostgreSQL, x86_64, 2 CPUs, Python 3.13.18):

| Fault injected | What Themis did | Reacted in | Journeys failing, before and after | Cause named correctly |
|---|---|---|---|---|
| `model_rate_limit` on `model:sim-large` | dependency quarantined, callers on fallback | 4.8s | 38% to 13% | 86 of 86 |
| `tool_wrong_result` on `tool:kb.search` | dependency quarantined, callers on fallback | 17.79s | 42% to 15% | 140 of 141 |
| `context_poison` on `agent:coder` | agent moved to suspended | 3.78s | 25% to 1% | 230 of 230 |
| `rogue_agent` on `agent:deployer` | agent moved to supervised | 3.06s | 5% to 2% | 179 of 179 |

Across all four, the cause was named correctly for **670 of 672** failed or stopped journeys. The rogue agent
got 1 unsafe action through before it was supervised and 1 after. Ingest took 11,757 spans a second on one process.

The models and tools in that run are simulated, so read the accuracy as an upper bound until it is measured
on your own traffic. The control plane in that run is the code in this repository.

## Does it stay up?

Themis has to keep the SLO it holds agents to. `scripts/soak.py` runs three replicas behind a load balancer on
production settings, with 40 journeys a second through the OpenTelemetry exporter, kills the leader with
SIGKILL, restarts another the way a rolling deploy does, and stops Postgres for 8 seconds. From
[docs/RELIABILITY.md](docs/RELIABILITY.md):

- **132 of 115,428 spans were lost**, all from exporters on the default 10 s timeout. On the 30 s timeout that
  [docs/INTEGRATION.md](docs/INTEGRATION.md) recommends, none were.
- **Nothing was counted twice**, even when the leader was killed in the middle of judging.
- A rolling deploy handed the evaluator over in **0.64 s**; a crashed leader was replaced in **43.47 s**, once
  its lease ran out.
- Outside the fault windows every route answered 100% of requests, with p95 between 22 and 64 ms.

The first run of the soak found a bug: replicas starting together on an empty database raced to create the
tables, and two of three crashed. That is fixed and has its own test.

## How it fits with the other repositories

| Repository | Its job | With Themis |
|---|---|---|
| [agent-harness-sdk](https://github.com/debanjan0212/agent-harness-sdk) | Build agents, OpenTelemetry built in | Its agents appear in Themis with no code change, and `ThemisPolicy` makes them obey it |
| [aegis-war-room](https://github.com/debanjan0212/aegis-war-room) | A team of agents investigates; a human says go or no-go | Receives Themis incidents, and reports its own nine agents to Themis |
| [argus-sre-agent](https://github.com/debanjan0212/argus-sre-agent) | From an alert to the root cause to a fix as a pull request | Receives Themis incidents, and reports its own investigations to Themis |
| [intentops](https://github.com/debanjan0212/intentops) | Describe a service once, run it anywhere | Plans and renders Themis's deployment |

Those make agents do SRE work. Themis does SRE work on the agents. `make ecosystem` clones all four at pinned
commits and tests each connection end to end; nothing in those tests is mocked.

## Security

Keys are scoped (agents, dashboard, operators, admins), stored as hashes and revocable. Tenants are isolated.
Telemetry is treated as untrusted input everywhere, including in the dashboard. Every rule has a test, and
[SECURITY.md](SECURITY.md) lists them next to the OWASP Top 10 for Agentic Applications, with the gaps.
`make scan` runs Bandit and pip-audit; CI adds gitleaks, Trivy and CodeQL.

## What it does not do yet

- Outside agent-harness-sdk the gate is advisory: a runtime that ignores it is not stopped. Enforce it where
  your agents call tools.
- Agents share one ingest key per tenant, so spans are not tied to an agent's identity.
- The measured results come from a simulator. No hosted model has been run through the benchmark yet.
- The container image is built and started by CI; it was not built on the machine this version was written on.

The plan for each of these is in [docs/ROADMAP.md](docs/ROADMAP.md).

## More

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how it works, high availability, the decisions and their costs |
| [docs/API.md](docs/API.md) | every route and scope |
| [docs/TELEMETRY.md](docs/TELEMETRY.md) | the span attributes Themis reads |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | your agents, the gate, the sibling projects, Prometheus |
| [docs/RELIABILITY.md](docs/RELIABILITY.md) | Themis held to its own SLO while it is broken on purpose |
| [CONTRIBUTING.md](CONTRIBUTING.md) | branches, what a change needs, where things are |

`make check` runs everything CI runs. `python scripts/film.py out/` re-records the demo video from a live run.
The product page in `site/` is built from the measured results by `python site/build.py` and published to
GitHub Pages by `.github/workflows/pages.yml`.

MIT licence. The typefaces, Sora and JetBrains Mono, are under the SIL Open Font Licence (`themis/web/fonts/`).
