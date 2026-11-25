# Reliability

Themis watches other systems' error budgets, so it has to keep its own. This page holds it to the SLO
in `deploy/intent.yaml` (99.9% available, p95 under 300 ms) while it is broken on purpose.

Every number here was measured by `scripts/soak.py` and written by it. Run it yourself with `make soak`.

## The setup

- 3 Themis replicas, each its own process, sharing one PostgreSQL, behind a load balancer that
  routes to ready replicas only (readiness is `/readyz`, as in Kubernetes)
- Production settings: 1h / 5m SLO windows, a 15 s evaluator interval, the default rate limit
- Load for 240 s: 40 journeys a second (480 spans a second) from 24 agent runtimes using the OpenTelemetry SDK and OTLP exporter, 100 runtimes polling the gate every second, 3 people watching the dashboard
- x86_64, 2 CPUs, Python 3.13.18. Replicas, database, load balancer and load generator all share these CPUs

## What was done to it

| When | What |
|---|---|
| 48 s | SIGKILL the leader (replica 1) |
| 92 s | new leader after crash (seconds 43.47) |
| 92 s | crashed replica restarted (replica 1) |
| 120 s | SIGTERM the leader (rolling deploy) (replica 0) |
| 121 s | new leader after clean stop (seconds 0.64, exit seconds 0.64) |
| 121 s | stopped replica restarted (replica 0) |
| 180 s | Postgres stopped (seconds 8.0) |
| 188 s | Postgres started |
| 241 s | load stopped (journeys 9619) |
| 241 s | exporters flushed (delivered 115296, dropped 132) |

## Was anything lost?

| | Count |
|---|---|
| Spans the agents created | 115,428 |
| Spans the exporters delivered | 115,296 |
| Spans the exporters gave up on | 132 |
| Spans stored by Themis | 115,296 |
| Journeys sent / judged | 9,619 / 9,608 |
| Agent tasks counted towards SLOs (expected 28,824) | 28,824 |
| Handoffs counted towards SLOs (expected 19,216) | 19,216 |

The 11 journeys not judged are exactly the spans given up on (11 x 12 = 132): an exporter drops a whole batch, so whole journeys go missing, not parts of them.

Half the agent runtimes used the OTLP exporter's default timeout and half the 30 s that
`docs/INTEGRATION.md` recommends. The timeout is how long an exporter keeps retrying a batch:

| Exporter timeout | Spans sent | Delivered | Given up on |
|---|---|---|---|
| 10s (the default) | 57,708 | 57,576 | 132 |
| 30s (`OTEL_EXPORTER_OTLP_TIMEOUT=30`) | 57,720 | 57,720 | 0 |

**132 of 115,428 spans (0.11%) were lost** through a crashed leader, a rolling restart and a database outage of 8 seconds. On the default 10 s timeout 132 were given up on; on 30 s, 0. Nothing was counted twice: every journey that arrived was judged exactly once, even when the leader died mid-judging.

## Themis's own SLO

Measured at the load balancer, like an ingress log. *Steady* is everything outside the fault windows; a fault
window runs from the moment a fault starts to 10 s after it has been undone.

| Route | Requests | Steady: available | Steady: p95 | Steady: p99 | During faults: available | During faults: p95 |
|---|---|---|---|---|---|---|
| `/api/journeys` | 273 | 100.00% | 25 ms | 46 ms | 88.00% | 33 ms |
| `/api/overview` | 446 | 100.00% | 64 ms | 142 ms | 90.68% | 98 ms |
| `/v1/gate/snapshot` | 22,616 | 100.00% | 22 ms | 46 ms | 89.69% | 28 ms |
| `/v1/traces` | 2,736 | 100.00% | 35 ms | 76 ms | 89.60% | 54 ms |

Requests refused during a fault were answered `503` with `Retry-After` or `502`, which OpenTelemetry
exporters retry. That is why the availability dips there and the span count above does not.

## Judging and failover

- A crashed leader was replaced in **43.47 s**. Its lease has to expire first (45 s, three intervals).
- A leader stopped for a rolling deploy handed over in **0.64 s**: it gives its lease up on the way out, and the other replicas check the lease every second.
- How long a finished journey waited to be judged: p50 15.7 s, p95 45.3 s, worst 45.3 s.
  Most of that is the evaluator interval itself. The worst case is the crash, while the old lease runs out.

## What this does not show

- One machine. A real cluster spreads replicas over nodes and zones; here they share CPUs with the database
  and the load, so latencies are higher than they would be on their own.
- An outage longer than the exporters' retry deadline (10 s) loses spans unless an OpenTelemetry Collector
  with a persistent queue sits in front. `deploy/otel-collector.yaml` is that configuration.
- Postgres itself was a single instance. Use a replicated, managed Postgres in production; its failover
  looks like the outage above.
