# Changelog

Every release, newest first. Versions follow semantic versioning.

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
