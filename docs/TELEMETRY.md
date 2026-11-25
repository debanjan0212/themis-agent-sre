# What Themis reads from a span

Themis accepts OTLP/HTTP at `POST /v1/traces` (protobuf or JSON, gzip allowed). It also answers `/v1/metrics`
and `/v1/logs` with 200 and stores nothing, because most exporters send all three signals to one address.

The OpenTelemetry GenAI semantic conventions are still at "Development" status, so names may change between
releases. This page is the full list of what Themis depends on.

## Standard attributes

| Attribute | Used for |
|---|---|
| `gen_ai.operation.name` | What the span is: `invoke_agent` or `create_agent` (an agent task), `chat`, `text_completion`, `generate_content` or `embeddings` (a model call), `execute_tool` (a tool call) |
| `gen_ai.agent.name` | Which agent. If a model or tool span does not carry it, Themis takes it from the nearest agent span above it in the trace |
| `gen_ai.request.model`, `gen_ai.response.model` | The model |
| `gen_ai.tool.name` | The tool. A span with this and no operation name is still a tool call |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | Tokens, and cost when the fleet manifest has a price for the model |
| span status `ERROR`, `error.type` | Failure, and why |
| `session.id`, `user.id` (on the root span) | The session and user a journey belongs to. `gen_ai.conversation.id` is used when there is no `session.id` |
| `k8s.pod.name`, `k8s.node.name` (span or resource), `service.instance.id` | Where it ran |
| `service.name` (resource) | Shown with the span |

A **journey** is one trace. The root span (the one with no parent) is the journey; its name is the journey's
name unless `themis.journey.name` says otherwise.

## Optional `themis.*` attributes

The conventions do not yet cover these. None is required. Each one you add makes the root cause sharper.

| Attribute | On | Meaning |
|---|---|---|
| `themis.journey.name` | root span | A stable name for the kind of journey (`ship_change`) |
| `themis.journey.outcome` | root span | `success`, `failed` or `blocked`. Use it when the result was wrong but nothing errored: a failed verification, a thumbs-down. Without it, the root span's status decides, except that `agent.run.status = escalated` (what agent-harness-sdk sets when a run hands its action to a person) reads as `blocked` |
| `themis.blocked.by` | root span | `agent:<name>` when the journey was stopped because that agent is suspended |
| `themis.handoff.from`, `themis.handoff.to` | a span around a handoff | Marks the span as a handoff between two agents |
| `themis.handoff.valid` | handoff span | `false` when the payload failed the receiver's schema. The sender is blamed |
| `themis.action.risk` | tool span | `high` for actions that change the world. Listing the tool under `high_risk` in the fleet manifest does the same |
| `themis.check.passed`, `themis.check.name` | any span | `false` when a check on this step failed: a result schema, a post-action verification |
| `themis.check.blame` | with a failed check | `caller` when the agent misused the tool. Otherwise the tool is blamed |
| `themis.gate.decision` | tool or handoff span | `deny` (outside the allow-list), `denied_by_human`, `approved_by_human` |
| `themis.gate.reason` | tool or handoff span | why the runtime stopped the action, in a sentence. Root cause quotes it |
| `themis.fallback` | model or tool span | `true` when the call went to the fallback because the first choice is quarantined |
| `themis.cost.usd` | model span | The cost, if you already know it |

## What is never stored

Unless `THEMIS_STORE_CONTENT=1`: `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
`gen_ai.prompt*`, `gen_ai.completion*`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`.

Always: more than 64 attributes on a span, attribute values past 256 characters, byte values, spans older than
24 hours or more than 5 minutes in the future, spans with malformed ids.

`sim.truth` is written only by the bundled simulator. It is kept to score the root cause in `themis bench` and
is never read by the root-cause code or shown in the API's span attributes.
