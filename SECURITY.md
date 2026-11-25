# Security

## Reporting a problem

Email **itsme.deb1995@gmail.com** with what you found and how to reproduce it.
Please do not open a public issue for anything exploitable.

## What Themis is trusted with

Themis sees what your agents do (which agent called which tool, when, at what cost) and it can take autonomy
away from them. So there are two things to protect: the telemetry, and the control.

It does **not** hold your model keys, your tools' credentials or your prompts. Prompts, completions and tool
payloads are dropped at ingest unless you set `THEMIS_STORE_CONTENT=1`.

## What is enforced, and where

The rules are in code, not in a prompt. Each one has a test that fails if it stops holding.

| Rule | Where | Test |
|---|---|---|
| Every route needs a key, except `/healthz`, `/readyz`, process-only `/metrics` and the static dashboard files | `themis/api.py` `need()` | `test_every_route_refuses_a_request_without_a_key` walks the real route table |
| Four scopes. An agent's key (`ingest`) cannot read the dashboard; a dashboard key (`read`) cannot see the fault plan or change anything | `themis/security.py` | `test_scopes` |
| Keys are 256 random bits, shown once, stored only as a SHA-256 hash, compared in constant time, revocable | `themis/security.py` | `test_keys_are_stored_as_hashes_and_can_be_revoked` |
| One tenant cannot see or change another tenant's agents, journeys, approvals or experiments | every query filters by the key's tenant | `test_one_tenant_cannot_see_or_touch_another` |
| Request bodies are capped before they are read, including chunked bodies and gzip bombs | `themis/api.py` | `test_bodies_over_the_limit_are_refused_before_they_are_read` |
| Malformed telemetry is a clean 400, never a crash | `themis/otlp.py` | fuzzed by `test_no_input_can_raise_anything_but_badpayload` |
| Spans from the far past or future are refused, so nobody can rewrite a closed SLO window | `themis/otlp.py` | `test_spans_that_would_corrupt_the_windows_are_rejected_not_stored` |
| Names from telemetry are never run as code in the dashboard | `themis/web/app.js` builds the page with text nodes only; CSP allows scripts from `'self'` only | `test_dashboard_views_work_and_telemetry_is_never_run_as_code` sends an agent named `<img onerror=…>` through real ingest |
| Webhooks cannot be pointed at internal addresses (SSRF) unless you allow it | `themis/security.py` `check_webhook_url` | `test_webhooks_to_private_addresses_are_refused_unless_allowed` |
| Webhooks are signed (HMAC-SHA256) when `THEMIS_WEBHOOK_SECRET` is set | `themis/engine.py` | `test_incidents_go_out_as_signed_alertmanager_webhooks` |
| A suspended agent is never un-suspended by a machine | `themis/autonomy.py` | `test_ladder_invariants` (property test) |
| An approval is decided once; no answer in time means no | `themis/api.py` | `test_approval_is_decided_once_and_expires_to_no` |
| Operator actions are written to an audit log with the key id, never the key | `themis/db.py` `audit` | `test_a_human_override_is_applied_logged_and_announced` |
| The root-cause code cannot read chaos experiments or simulator labels | `themis/rca.py` imports nothing from the project | `test_rca_module_cannot_see_chaos_or_the_database` |

Rate limiting is per key and per replica (`THEMIS_RATE_LIMIT`).

## Scans

`make scan` runs what CI runs:

- **Bandit** on `themis/` (medium or worse fails). Two low findings are accepted with a reason in the code:
  the simulator uses a seeded random generator, which is the point of a simulator.
- **pip-audit** on `requirements.lock`, which pins every dependency by hash.
- In CI also **gitleaks** over the full history, **Trivy** (dependencies, secrets, Dockerfile and Kubernetes
  config) and **CodeQL** for Python and the dashboard's JavaScript.

Every GitHub Action is pinned to a commit SHA. The container runs as user 65532 with a read-only root
filesystem, no capabilities and no service-account token; `deploy/kubernetes` adds a network policy that allows
egress only to DNS, Postgres and the incident receivers you declare.

No secret-looking string is committed anywhere, including tests: keys are generated at run time.

## How this maps to the OWASP Top 10 for Agentic Applications (2026)

Themis does not run your agents, so it does not prevent these on its own. It detects them, limits the damage
and tells you which agent it was. The table says which, honestly.

| Risk | What Themis does about it |
|---|---|
| ASI01 Agent Goal Hijack | **Limits damage.** A hijacked agent that reaches outside its allow-list is denied by the gate; five denials in the short window suspend it. The `context_poison` chaos fault tests exactly this. It does not read prompts, so it cannot see the hijack itself |
| ASI02 Tool Misuse and Exploitation | **Limits damage.** Per-agent tool allow-lists; high-risk tools need a human go once the agent is supervised; failed post-action checks burn the `safe_actions` budget at 99.9% |
| ASI03 Identity and Privilege Abuse | **Partly.** Scoped keys and tenant isolation for Themis itself. **Gap:** agents share one `ingest` key per tenant, so an agent could report spans in another agent's name |
| ASI04 Agentic Supply Chain Vulnerabilities | **For Themis itself:** hashed lock file, pinned actions, weekly scans. Nothing for your agents' own dependencies |
| ASI05 Unexpected Code Execution | **Not Themis's layer**, except that Themis never executes anything it receives: telemetry is data, and the dashboard never builds HTML from it |
| ASI06 Memory and Context Poisoning | **Detects the effect**, as ASI01. Silent wrong answers from a poisoned source are found by the statistics in `themis/rca.py` when there is enough traffic |
| ASI07 Insecure Inter-Agent Communication | **Detects.** Handoff allow-lists and a `handoff_success` budget per sender. **Gap:** handoffs are not signed |
| ASI08 Cascading Failures | **Core job.** Error-budget demotion, dependency quarantine with back-off, and blast-radius scoring in every chaos experiment |
| ASI09 Human-Agent Trust Exploitation | **Partly.** The approver sees the action and whether its safety check passed, and every decision is audited. **Gap:** no second approver for the riskiest actions |
| ASI10 Rogue Agents | **Core job.** The `rogue_agent` fault: demoted on the first burn, every unsafe action then stopped by a human, suspended if it persists |

## Known gaps

- One `ingest` key per tenant, not per agent (ASI03 above). Per-agent keys are the first item on the roadmap.
- The gate is advisory: an agent runtime that ignores the snapshot is not stopped by Themis. Enforce it where
  your agents call tools (see `docs/INTEGRATION.md`), or put the tools behind a proxy that asks the gate.
- A revoked key keeps working on other replicas for up to 5 seconds (their cache).
- Rate limiting starts after a key is accepted. Requests with made-up keys are refused cheaply but are not
  counted, so put the usual ingress rate limit in front of Themis.
- The webhook address check resolves the host just before sending; it does not pin the resolved address.
