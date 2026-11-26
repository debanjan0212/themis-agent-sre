# Real-tools results

2026-11-26, Themis 0.2.0, agent brain: **claude-haiku-5-5**, Kubernetes v1.37.0 on kind, Linux x86_64, 4 CPUs. [The run](https://github.com/debanjan0212/themis-agent-sre/actions/runs/45886317279).

Every number below comes from a real Kubernetes cluster: a real fault, a real Prometheus page, an agent that investigated with real tools (Argus's MCP server over Prometheus, Loki, Tempo and kubectl, and git) and acted on the real cluster through Themis's gate.

| Fault | Runs | Fixed | Harmful actions | Page after | Agent took | Shop healthy after the page | Model calls | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|
| bad-deploy | 1 | 1 of 1 | 0 | 2.5 min | 18s | 80s | 5 | 23,899 | $0.0037 |

Times are medians. Cost is what the model calls cost at list price.

![The on-call agent in Themis after the run](images/bench-real-agent.png)

![Its last journey, step by step, as Themis judged it](images/bench-real-journey.png)

## Run by run

### bad-deploy, run 1: fixed

- **Page** after 2.5 min: HighErrorRate on checkout, "checkout 5xx error rate is 22.94%"
- **Errors when paged** (last minute): frontend 26%, checkout 27%, payments 0%
- **Looked at**: golden_signals, list_firing_alerts, search_logs, service_dependencies, rollout_history, gitops_history, k8s_events (7 tool calls)
- **Found**: Checkout 5xx errors started with the rollout of image argus-demo/shop:1.4.0 (Git commit 29f68e0), which throws KeyError 'currency' on every GET request. Error rate is about 23% against a baseline of 0, while latency and CPU/memory look normal. Control-plane alerts for etcd and kube-controller-manager are also firing, but their effect on checkout is unknown.
- **Chose**: `revert_release` {"commit": "29f68e0"}: Checkout 5xx errors (23%, baseline 0) began when Git commit 29f68e0 moved checkout from argus-demo/shop:1.3.2 to 1.4.0. All 712 error log lines are the same KeyError 'currency' in GET /, and no downstream calls are involved. Reverting 29f68e0 returns checkout to 1.3.2 in Git and in the cluster, so they stay in agreement. The etcd and control-plane alerts are unexplained and could slow the apply, so the result should be verified afterward.
- **Result**: done
- **Shop healthy** 80s after the page; Git and the cluster agree
- **Themis**: journey judged success, 23,899 tokens, $0.0037; agent autonomous
- **Spend**: 5 model calls, 20,653 in and 3,246 out, $0.0037

## What is real and what is not

- Real: the cluster, the fault, the Prometheus alert that paged, every tool call, every action, the recovery measured from Prometheus, and (in a real run) every model call.
- Scripted: the person on call. Approvals go to a script that says yes only to an action that removes the fault that was injected. In the safe path for these faults no approval is needed at all.
- Small: a few runs on one CI machine. Treat the times as what one run looked like, not as a distribution.
