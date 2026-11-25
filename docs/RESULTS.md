# Results

Every number on this page was measured by `themis bench` and written by it. Nothing here is typed by hand.
Run it yourself: `make bench`. The same seed gives the same traffic; timings vary a little with the machine.

- Themis 0.1.0, postgresql+psycopg database, x86_64, 2 CPUs, Python 3.13.18
- 16 simulated users, seed 7, 270s wall time, 3,862 journeys
- Time is compressed 120x: the 1h / 5m burn-rate windows are 30s / 7.5s here
- Healthy baseline: 98.9% success, p95 990 ms, 728 journeys/min

The models and tools are simulated. The control plane is not: the spans, the SLO maths, the gate, the
approvals, the quarantine and the root-cause analysis are the same code that runs in production.

## Four faults, one after another

| Fault | Target | How Themis responded | Detected in | Failed rate before → after response | Unsafe actions before → after |
|---|---|---|---|---|---|
| `model_rate_limit` at 80% | `model:sim-large` | dependency quarantined, callers on fallback | 3.96s | 30.6% → 10.4% | 0 → 0 |
| `tool_wrong_result` at 60% | `tool:kb.search` | dependency quarantined, callers on fallback | 15.37s | 45.0% → 14.2% | 0 → 0 |
| `context_poison` at 60% | `agent:coder` | agent moved to suspended | 4.7s | 19.1% → 1.6% | 0 → 0 |
| `rogue_agent` at 30% | `agent:deployer` | agent moved to supervised | 3.99s | 5.6% → 2.2% | 2 → 1 |

## Root cause accuracy

The simulator knows the true cause of every failed journey. The root-cause code never sees that label;
it is only used here to mark the answers.

| Fault | Journeys the fault broke | Themis named the target | by evidence | by statistics | named something else | no answer |
|---|---|---|---|---|---|---|
| `model_rate_limit` | 57 | 57 (100.0%) | 57 | 0 | 0 | 0 |
| `tool_wrong_result` | 133 | 128 (96.2%) | 30 | 98 | 5 | 0 |
| `context_poison` | 230 | 230 (100.0%) | 230 | 0 | 0 | 0 |
| `rogue_agent` | 164 | 164 (100.0%) | 164 | 0 | 0 | 0 |

Across every failed or blocked journey in the four experiments (injected faults and ordinary background
failures together): **632 of 638 named correctly (99.1%)**.

## Blast radius

| Fault | Journeys during the fault | Failed | Safely blocked | Agents in failed journeys | Users affected |
|---|---|---|---|---|---|
| `model_rate_limit` | 579 | 73 | 0 | 10 | 16 |
| `tool_wrong_result` | 571 | 148 | 0 | 10 | 16 |
| `context_poison` | 680 | 23 | 218 | 7 | 12 |
| `rogue_agent` | 588 | 15 | 161 | 9 | 11 |

## Ingest

60,000 spans in OTLP/HTTP protobuf, 1000 per request, 4 clients: **15,790 spans/s**, request p50 251.5 ms, p95 323.7 ms.
One API process, with the load generator in the same process, so this is a floor and not a ceiling.

## What these numbers do not show

- Real models fail in messier ways than a simulator. Treat the accuracy as an upper bound until it is
  measured on your own traffic.
- Root cause by evidence works where a boundary check exists (a schema check, a gate decision, a
  post-action verification). Where none exists Themis falls back to statistics, which need volume.
- Detection time is mostly the SLO window, on purpose: it is the same 14.4x / 1h / 5m rule used for
  services. Policy violations and quarantine do not wait for a window.
