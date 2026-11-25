# Configuration

Everything is an environment variable. The defaults are the production values.

| Variable | Default | What it does |
|---|---|---|
| `THEMIS_DB_URL` | `sqlite:///themis.db` | Where data lives. For more than one replica use Postgres: `postgresql+psycopg://user@host/themis` (install with `pip install -e ".[postgres]"`) |
| `THEMIS_BOOTSTRAP_KEY` | none | An admin key to accept from the first start, usually from a Kubernetes Secret. Create one with `themis key` and store the printed value. Only its hash is written to the database |
| `THEMIS_EXTERNAL_URL` | `http://localhost:8600` | The address people use to open the dashboard. Put in webhook links |
| `THEMIS_WEBHOOKS` | none | Comma-separated URLs that receive incidents in Alertmanager webhook format |
| `THEMIS_WEBHOOK_TOKEN` | none | Sent as `Authorization: Bearer …` to every webhook (Aegis's `AEGIS_API_TOKEN`, for example) |
| `THEMIS_WEBHOOK_SECRET` | none | When set, every webhook carries `X-Themis-Signature: sha256=<hmac of the body>` |
| `THEMIS_WEBHOOK_ALLOW_PRIVATE` | `0` | Webhooks to private, loopback or link-local addresses are refused unless this is `1`. Set it when the receiver is a Service inside the same cluster |
| `THEMIS_STORE_CONTENT` | `0` | `1` keeps prompt, completion and tool-payload attributes. Off by default because they can hold customer data |
| `THEMIS_RETENTION_HOURS` | `72` | Spans, journeys and gate events older than this are deleted |
| `THEMIS_RATE_LIMIT` | `300` | Requests per second per key, per replica |
| `THEMIS_MAX_BODY_BYTES` | `4000000` | Largest request body accepted |
| `THEMIS_EVAL_INTERVAL` | `15` | Seconds between evaluator runs. Leader failover takes three of these (at least 5 seconds) |
| `THEMIS_SETTLE_SECONDS` | `2` | How long to wait after a journey's root span ends before judging it, so late child spans are included |
| `THEMIS_WINDOW_SCALE` | `1` | Time compression for demos. `themis demo` uses 120 with a 1-second evaluator, which turns the 1h / 5m windows into 30s / 7.5s. Leave at 1 in production |
| `THEMIS_CORS_ORIGINS` | none | Only needed if the dashboard is served from another origin |
| `THEMIS_INSTANCE` | hostname + random | This replica's name in the evaluator lease |

## Keys and scopes

```bash
themis key --scopes ingest            # for agents: send spans, read the gate snapshot, ask for approvals
themis key --scopes read              # for the dashboard and for Prometheus
themis key --scopes read,operate      # for on-call: approve, demote, reinstate, run chaos
themis key --scopes admin             # everything, plus keys and the fleet manifest
themis key --scopes ingest --tenant team-payments   # a separate tenant
```

A key is printed once. Later keys can also be made with `POST /api/keys` using an admin key.

## The fleet manifest

`themis/world/fleet.yaml` is a complete example. For your own agents, write one and apply it with
`PUT /api/fleet` (admin scope). Keep it in Git: it is the policy.

```yaml
prices:                      # USD per 1M input and output tokens, used when a span does not carry its cost
  my-model: [3.0, 15.0]
fallbacks:                   # where callers go while a model is quarantined
  my-model: my-cheaper-model
agents:
  deployer:
    model: my-model
    tools: [gitops.open_pr, deploy.apply]   # anything else is denied by the gate
    high_risk: [deploy.apply]               # needs a human go while the agent is supervised
    handoffs: [scribe]
    cost_ceiling: 0.02                      # USD per task; turns on the cost indicator
    slo: {task_success: 0.97, safe_actions: 0.999}   # optional, these are the defaults
```

An agent that is not in the manifest is still observed and judged with the default targets. It just has no
allow-list, so the gate does not restrict it.
