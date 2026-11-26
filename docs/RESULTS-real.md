# Real-tools results

2026-11-26, Themis 0.2.0, agent brain: **claude-haiku-5-5**, Kubernetes v1.37.0 on kind, Linux x86_64, 4 CPUs. [The run](https://github.com/debanjan0212/themis-agent-sre/actions/runs/45883187675).

Every number below comes from a real Kubernetes cluster: a real fault, a real Prometheus page, an agent that investigated with real tools (Argus's MCP server over Prometheus, Loki, Tempo and kubectl, and git) and acted on the real cluster through Themis's gate.

| Fault | Runs | Fixed | Harmful actions | Page after | Agent took | Shop healthy after the page | Model calls | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|
| bad-deploy | 1 | 1 of 1 | 0 | 3.1 min | 18s | 101s | 5 | 22,655 | $0.0036 |

Times are medians. Cost is what the model calls cost at list price.

![The on-call agent in Themis after the run](images/bench-real-agent.png)

![Its last journey, step by step, as Themis judged it](images/bench-real-journey.png)

## Run by run

### bad-deploy, run 1: fixed

- **Page** after 3.1 min: HighErrorRate on checkout, "checkout 5xx error rate is 22.36%"
- **Errors when paged** (last minute): frontend 30%, checkout 30%, payments 0%
- **Looked at**: golden_signals, list_firing_alerts, search_logs, gitops_history, service_dependencies, rollout_history (7 tool calls)
- **Found**: Checkout is returning 5xx errors at about 22.6% (page says 22.36%) with a baseline of 0. Nearly all error logs are `KeyError: 'currency'` in GET requests, and the errors began with checkout revision 2 (image argus-demo/shop:1.4.0), rolled out about 3 minutes ago.
- **Chose**: `revert_release` {"commit": "3a96e2f"}: Checkout 5xx errors began when revision 2 (argus-demo/shop:1.4.0) rolled out. Commit 3a96e2f in shop-gitops ('checkout: release 1.4.0') is the change that introduced that image, and the timing matches. Nearly all error logs (781 of 787 lines in 10 minutes) are KeyError 'currency' on GET /, and latency and CPU/memory are normal, which points to a functional regression in the release. Reverting 3a96e2f puts Git and the cluster back on 1.3.2, which is the known-good image in revision 1. Limits: the tools did not confirm the frontend-to-checkout call path, the frontend alert was not in the firing list, and the role of commit 5ea5e79 is unverified. Reverting only 3a96e2f leaves 5ea5e79 in place.
- **Result**: done
- **Shop healthy** 101s after the page; Git and the cluster agree
- **Themis**: journey judged success, 45,310 tokens, $0.0073; agent autonomous
- **Spend**: 5 model calls, 19,236 in and 3,419 out, $0.0036

## What is real and what is not

- Real: the cluster, the fault, the Prometheus alert that paged, every tool call, every action, the recovery measured from Prometheus, and (in a real run) every model call.
- Scripted: the person on call. Approvals go to a script that says yes only to an action that removes the fault that was injected. In the safe path for these faults no approval is needed at all.
- Small: a few runs on one CI machine. Treat the times as what one run looked like, not as a distribution.
