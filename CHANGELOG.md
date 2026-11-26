# Changelog

Every release, newest first. Versions follow semantic versioning.

## Unreleased

- **`themis bench-real`:** real faults on a real kind cluster, worked by an on-call agent on agent-harness-sdk with
  real tools (Argus's MCP server over Prometheus, Loki, Tempo and kubectl, and git), acting through the gate. Runs
  on Claude Haiku from `.github/workflows/bench-real.yml` through workload identity federation, with a hard cap on
  model calls; pull requests run it with a scripted playbook to prove the plumbing. The first fault is a bad
  release shipped through Git.
- **Fixed, found by bench-real:** a journey's tokens were counted twice for agent-harness-sdk agents, because the
  GenAI conventions put the run's total on the `invoke_agent` span as well as on each model call. Only model calls
  count now, when there are any.
- **First results on Claude Haiku** in `docs/RESULTS-real.md`: two runs of the bad release, both fixed by a
  revert in Git, the shop healthy 101 s and 80 s after the page, about 23,000 tokens and $0.0037 a run.
- `scripts/film_real.py` films one bench-real incident as a narrated demo from the run's own records, and replays
  Themis's dashboard from the run's database.
- **Fixed, found by the first Claude run:** each model call was counted twice, so the run's tokens and cost
  showed double. agent-harness-sdk traces every call, and the anthropic package traces the same call again inside
  it. A model span inside another model span is now one call: it is priced once, counted once in the journey, the
  error budget and the fleet map, and still shown in the journey's steps. LangChain with OpenAI's instrumentation
  nests the same way.
- **Fixed, found by bench-real's screenshots:** an MCP client traces its own handshakes (`tools/list`,
  `server/discover`) before any agent runs, and each one showed up as a journey. A trace with no agent, model or
  tool in it, and no journey name, is now treated as plumbing and not judged.
- bench-real pages only on the shop's own alerts, records each run's error rate over time, photographs Themis's
  dashboard at the end, and a dispatched run pushes its results to a `bench-real/run-<id>` branch.
- The sibling pins live in `scripts/pins.sh`, shared by the ecosystem tests and bench-real. agent-harness-sdk is
  pinned to the commit that adds `AnthropicModel`.

## 0.2.0 (25 November 2026)

Held to its own SLO, and connected to the sibling projects both ways.

- **Measured reliability:** `scripts/soak.py` runs three replicas behind a load balancer on production settings,
  kills the leader, does a rolling restart and stops Postgres, and writes `docs/RELIABILITY.md`. In the committed
  run 132 of 115,428 spans were lost, all on the exporter's default 10 s timeout and none on 30 s, and nothing
  was counted twice.
- **Fixed, found by the soak:** replicas starting together on an empty Postgres raced to create the tables and
  all but one crashed. Schema creation now takes an advisory lock.
- **Failover:** a replica stopped for a rolling deploy gives its lease up and the others check every second, so
  the evaluator moves in under a second. Each process holds the lease under its own name, so two replicas
  configured with the same name can never both lead. A journey is claimed before it is judged, so a leader that
  lost the lease mid-tick cannot count it twice.
- **Overload and outages:** a database that is down or out of connections is answered `503` with
  `Retry-After`, which OpenTelemetry exporters retry, instead of `500`. One trace that cannot be judged no longer
  stops the others, and a slow incident receiver no longer holds up the evaluator.
- **Faster dashboard:** what-uses-what is pre-aggregated per minute, so the overview stays fast as spans pile
  up. **Metrics:** the HTTP histogram now uses the OpenTelemetry names (`http_server_request_duration_seconds`),
  which the SLO rules IntentOps renders already read, plus evaluator lag, journeys judged, traces skipped and
  webhook deliveries. `deploy/alerts.yaml` alerts on Themis's own health; `deploy/otel-collector.yaml` is a
  Collector with a disk queue for outages longer than an exporter retries.
- **agent-harness-sdk obeys Themis:** `ThemisPolicy` and `ThemisApprover` in the SDK. Ecosystem tests run them
  against the control plane: violations suspend the agent, the SDK stops its actions, a supervised action waits
  for an approval in Themis. An escalated harness run reads as blocked, and root cause quotes the runtime's
  reason for a block.
- **Aegis War Room and Argus report their own agents** over OpenTelemetry, checked by the ecosystem tests.
- **A new dashboard:** near-black glass panels, and a fleet map drawn as an instrument: segmented rings that
  turn and tilt like a gyroscope's, the human as a lit sphere at the centre, and traffic running along the lines
  as light, red where calls fail. Vitals carry a sparkline of the last few minutes. Sora and JetBrains Mono. All
  motion stops for anyone who has asked for reduced motion, and every moving ring is its own layer, so the map is
  not redrawn on each frame. The dashboard no longer prints "null" where a part is missing.
- **A product page:** `site/` is built only from measured numbers, each footnoted, and published by
  `.github/workflows/pages.yml`. `tests/test_design.py` checks it against the 20 signs of a generated page.
- **Demo video:** `scripts/film.py` films with Chrome's own screencast, about five times the frame rate of
  screenshots, and adds a card for the soak results.

## 0.1.0 (25 November 2026)

The first working version.

- **Ingest:** OTLP/HTTP traces (protobuf, JSON, gzip) read by the OpenTelemetry GenAI conventions. Prompts and
  tool payloads are dropped by default.
- **Journeys:** every user request followed across agents, models, tools and pods, with cost and tokens.
- **Error-budget autonomy:** four indicators per agent, the 14.4x / 6x multi-window burn rules, and a ladder of
  autonomous, supervised and suspended. Only a person un-suspends an agent.
- **The gate:** a snapshot runtimes poll for autonomy, allow-lists, quarantines and faults; human go / no-go
  for high-risk actions; five policy denials suspend an agent.
- **Quarantine:** tools and models that keep causing failures are taken out with a doubling time limit.
- **Root cause:** from span evidence first, from statistics for failures that leave no evidence.
- **Chaos:** `model_rate_limit`, `tool_wrong_result`, `context_poison`, `rogue_agent` and `latency`, each scored
  for detection, containment, blast radius and root-cause accuracy.
- **Incidents:** Alertmanager-format webhooks, signed, at least once. Tested against Aegis War Room, Argus and
  Incident Commander running from their own repositories.
- **Operations:** multi-tenant keys with four scopes, audit log, SQLite or Postgres, stateless API replicas and
  a leader-elected evaluator, Prometheus metrics, Kubernetes manifests rendered by IntentOps.
- **Dashboard:** the fleet map (agents orbit closer to the human as they lose autonomy), journeys, agents,
  chaos lab and incidents. No build step.
- **World:** a seeded ten-agent simulator that emits real OTLP and obeys the gate, used by `themis demo`,
  `themis bench` and the system tests.
