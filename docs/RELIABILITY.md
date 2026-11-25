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
| 103 s | new leader after crash (seconds 55.42) |
| 103 s | crashed replica restarted (replica 1) |
| 120 s | SIGTERM the leader (rolling deploy) (replica 2) |
| 135 s | new leader after clean stop (seconds 14.63, exit seconds 14.63) |
| 135 s | stopped replica restarted (replica 2) |
| 180 s | Postgres stopped (seconds 8.0) |
| 188 s | Postgres started |
| 241 s | load stopped (journeys 9619) |
| 241 s | exporters flushed (delivered 115272, dropped 156) |

## Was anything lost?

| | Count |
|---|---|
| Spans the agents created | 115,428 |
| Spans the exporters delivered | 115,272 |
| Spans the exporters gave up on | 156 |
| Spans stored by Themis | 115,272 |
| Journeys sent / judged | 9,619 / 9,606 |
| Agent tasks counted towards SLOs (expected 28,818) | 28,818 |
| Handoffs counted towards SLOs (expected 19,212) | 19,212 |

**156 spans were lost** through a crashed leader, a rolling restart and a 8-second database outage. Nothing was counted twice: every journey was judged exactly once, even when the leader died in the middle of judging.

## Themis's own SLO

Measured at the load balancer, like an ingress log. *Steady* is everything outside the fault windows; a fault
window runs from the moment a fault starts to 10 s after it has been undone.

| Route | Requests | Steady: available | Steady: p95 | Steady: p99 | During faults: available | During faults: p95 |
|---|---|---|---|---|---|---|
| `/api/journeys` | 273 | 100.00% | 35 ms | 70 ms | 92.97% | 63 ms |
| `/api/overview` | 426 | 100.00% | 238 ms | 278 ms | 91.05% | 213 ms |
| `/v1/gate/snapshot` | 22,381 | 100.00% | 35 ms | 86 ms | 91.76% | 64 ms |
| `/v1/traces` | 2,726 | 100.00% | 53 ms | 103 ms | 92.17% | 92 ms |

Requests refused during a fault were answered `503` with `Retry-After` or `502`, which OpenTelemetry
exporters retry. That is why the availability dips there and the span count above does not.

## Judging and failover

- A crashed leader was replaced in **55.42 s**. Its lease has to expire first (45 s, three intervals).
- A leader stopped for a rolling deploy handed over in **14.63 s**: it gives its lease up on the way out, so the next replica takes over on its next interval.
- How long a finished journey waited to be judged: p50 16.5 s, p95 59.2 s, worst 59.2 s.
  Most of that is the evaluator interval itself. The worst case is the crash, while the old lease runs out.

## What this does not show

- One machine. A real cluster spreads replicas over nodes and zones; here they share CPUs with the database
  and the load, so latencies are higher than they would be on their own.
- An outage longer than the exporters' retry deadline (10 s) loses spans unless an OpenTelemetry Collector
  with a persistent queue sits in front. `deploy/otel-collector.yaml` is that configuration.
- Postgres itself was a single instance. Use a replicated, managed Postgres in production; its failover
  looks like the outage above.
