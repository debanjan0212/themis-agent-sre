# Connecting things to Themis

Three connections, each optional and each useful on its own.

| You want | You do | Themis then |
|---|---|---|
| To **see** your agents | Point their OTLP exporter at Themis | shows journeys, costs, what uses what, SLOs and root causes |
| Themis to **control** them | Ask the gate before each action | demotes, suspends, quarantines, and your runtime obeys |
| People to **hear about it** | Set `THEMIS_WEBHOOKS` | opens incidents in Aegis War Room, Argus, Incident Commander or anything that reads Alertmanager webhooks |

## 1. See: any OpenTelemetry agent

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://themis:8600
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20$THEMIS_INGEST_KEY"   # %20 is the space, URL-encoded
```

That is all for an agent built with [agent-harness-sdk](https://github.com/debanjan0212/agent-harness-sdk): it
already emits GenAI spans. `tests/test_ecosystem.py` runs the SDK's quickstart and its Incident Commander with
only these two variables set and checks that both show up with their tools, models and tokens.

For another framework, emit the standard GenAI attributes (`docs/TELEMETRY.md`). Start a root span per user
request and Themis has its journeys.

## 2. Control: the gate

With [agent-harness-sdk](https://github.com/debanjan0212/agent-harness-sdk) it is two arguments:

```python
from agent_harness_sdk import Agent, ThemisApprover, ThemisGate, ThemisPolicy

gate = ThemisGate("http://themis:8600", ingest_key).start()
agent = Agent("deployer", model=..., tools=[...], policy=ThemisPolicy(gate=gate), approver=ThemisApprover(gate))
```

Supervised, every action waits for a person in the Themis dashboard; suspended, every action goes to a person
instead; a tool outside the allow-list is blocked and reported, and five of those suspend the agent.
`test_agent_harness_sdk_agents_obey_themis` runs exactly that against the real control plane: five reaches
outside the allow-list, Themis suspends the agent, the SDK stops its next action, and a supervised agent's action
runs only after an approval is given through the Themis API.

Any other runtime uses the same client directly:

```python
from themis.client import Gate

gate = Gate("http://themis:8600", ingest_key)
gate.start()  # refreshes a snapshot every second, in the background


async def call_tool(agent: str, tool: str, args: dict):
    if gate.autonomy(agent) == "suspended":
        raise AgentSuspended(agent)  # do not run the agent at all
    if not gate.allowed(agent, "tools", tool):
        gate.report(agent, f"tool:{tool}", "deny", "not in the allow-list")  # five of these suspend it
        raise PermissionError(tool)
    if gate.high_risk(agent, tool) and gate.autonomy(agent) == "supervised":
        if not await gate.approve(agent, f"tool:{tool}", {"args": args}):  # waits for a human; no answer is a no
            raise ApprovalDenied(tool)
    if gate.quarantined(f"tool:{tool}"):
        tool = my_fallback_for(tool)
    return await run(tool, args)
```

`themis/world/sim.py` is a complete runtime written this way; `chat()` and `tool()` are the two functions to
replace with real calls. The same snapshot carries chaos faults (`gate.fault(name, target)`), so a runtime that
honours them can be tested with `themis chaos` in staging.

If Themis is down, the gate keeps its last snapshot and your agents keep working. Only `approve()` fails closed.

## 3. Tell people: the sibling projects

Themis posts every demotion, suspension and quarantine as an Alertmanager webhook (version 4). The agent or
component name is in the `service` and `component` labels; `fingerprint` is the incident id.

```bash
export THEMIS_WEBHOOKS="http://aegis:8000/api/alerts,http://argus:8080/alertmanager,http://commander:8080/webhooks/alertmanager"
export THEMIS_WEBHOOK_TOKEN="$AEGIS_API_TOKEN"      # Aegis checks it when AEGIS_API_TOKEN is set
export THEMIS_WEBHOOK_ALLOW_PRIVATE=1               # the receivers are inside your cluster
```

| Project | What happens with a Themis incident | Checked by |
|---|---|---|
| [Aegis War Room](https://github.com/debanjan0212/aegis-war-room) | Opens a war-room incident: agents investigate, the defender checks the fix, a human says go or no-go | `test_an_incident_reaches_aegis_argus_and_incident_commander` starts all three from their repositories and sends one real incident |
| [Argus](https://github.com/debanjan0212/argus-sre-agent) | Starts a root-cause investigation filed under the Themis incident id | same test |
| Incident Commander (in agent-harness-sdk) | Opens and handles an incident | same test |
| [IntentOps](https://github.com/debanjan0212/intentops) | Plans, policy-checks and renders Themis's own Kubernetes deployment from `deploy/intent.yaml` | `test_intentops_plans_checks_and_renders_the_themis_deployment` fails if `deploy/kubernetes` is out of date |

The other direction works too: the agents inside Aegis, Argus and Incident Commander are agents like any other.
Set `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS` for them (step 1) and the things that respond to
incidents are themselves under an error budget. Aegis sends two traces per incident (investigate, then act, with
the human's go as an approved tool call), Argus one per investigation; `test_aegis_and_argus_report_their_own_agents`
checks that Themis sees all ten of their agents.

`make ecosystem` clones the four repositories at pinned commits and runs these tests.

## Prometheus and Grafana

`GET /metrics` with a `read` key returns each agent's autonomy level, burn rates and budget left, so the LGTM
stack you already run can chart and alert on them. Without a key it returns only the process's own health.
For traces, keep sending to Tempo as well: an OpenTelemetry Collector can export to both.
