# Results

Every number on this page was measured by `themis bench` and written by it. Nothing here is typed by hand.
Run it yourself: `make bench`. The same seed gives the same traffic; timings vary a little with the machine.

- Themis 0.2.0, postgresql+psycopg database, x86_64, 2 CPUs, Python 3.13.18
- 16 simulated users, seed 7, 279s wall time, 3,955 journeys
- Time is compressed 120x: the 1h / 5m burn-rate windows are 30s / 7.5s here
- Healthy baseline: 98.9% success, p95 1015 ms, 720 journeys/min

The models and tools are simulated. The control plane is not: the spans, the SLO maths, the gate, the
approvals, the quarantine and the root-cause analysis are the same code that runs in production.

## Four faults, one after another

| Fault | Target | How Themis responded | Detected in | Failed rate before → after response | Unsafe actions before → after |
|---|---|---|---|---|---|
| `model_rate_limit` at 80% | `model:sim-large` | dependency quarantined, callers on fallback | 4.8s | 38.2% → 12.8% | 0 → 0 |
| `tool_wrong_result` at 60% | `tool:kb.search` | dependency quarantined, callers on fallback | 17.79s | 41.6% → 14.8% | 0 → 0 |
| `context_poison` at 60% | `agent:coder` | agent moved to suspended | 3.78s | 25.0% → 1.1% | 0 → 0 |
| `rogue_agent` at 30% | `agent:deployer` | agent moved to supervised | 3.06s | 4.9% → 1.8% | 1 → 1 |

## Root cause accuracy

The simulator knows the true cause of every failed journey. The root-cause code never sees that label;
it is only used here to mark the answers.

| Fault | Journeys the fault broke | Themis named the target | by evidence | by statistics | named something else | no answer |
|---|---|---|---|---|---|---|
| `model_rate_limit` | 86 | 86 (100.0%) | 86 | 0 | 0 | 0 |
| `tool_wrong_result` | 141 | 140 (99.3%) | 29 | 111 | 1 | 0 |
| `context_poison` | 230 | 230 (100.0%) | 230 | 0 | 0 | 0 |
| `rogue_agent` | 179 | 179 (100.0%) | 179 | 0 | 0 | 0 |

Across every failed or blocked journey in the four experiments (injected faults and ordinary background
failures together): **670 of 672 named correctly (99.7%)**.

## Blast radius

| Fault | Journeys during the fault | Failed | Safely blocked | Agents in failed journeys | Users affected |
|---|---|---|---|---|---|
| `model_rate_limit` | 574 | 93 | 0 | 9 | 16 |
| `tool_wrong_result` | 574 | 152 | 0 | 9 | 16 |
| `context_poison` | 668 | 21 | 216 | 7 | 13 |
| `rogue_agent` | 588 | 12 | 178 | 9 | 8 |

## Ingest

60,000 spans in OTLP/HTTP protobuf, 1000 per request, 4 clients: **11,757 spans/s**, request p50 324.8 ms, p95 449.2 ms.
One API process, with the load generator in the same process, so this is a floor and not a ceiling.

## What these numbers do not show

- Real models fail in messier ways than a simulator. Treat the accuracy as an upper bound until it is
  measured on your own traffic.
- Root cause by evidence works where a boundary check exists (a schema check, a gate decision, a
  post-action verification). Where none exists Themis falls back to statistics, which need volume.
- Detection time is mostly the SLO window, on purpose: it is the same 14.4x / 1h / 5m rule used for
  services. Policy violations and quarantine do not wait for a window.
