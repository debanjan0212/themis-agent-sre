# API

Send the key as `Authorization: Bearer <key>`. `admin` includes every other scope. Errors are JSON:
`{"detail": "..."}` with 400 (bad input), 401 (no or wrong key), 403 (wrong scope), 404, 409 (conflict),
411 / 413 (body rules), 422 (validation) or 429 (rate limit, with `Retry-After`).

## For agents and their runtimes (scope `ingest`)

| Route | What it does |
|---|---|
| `POST /v1/traces` | OTLP/HTTP trace export, protobuf or JSON, optionally gzip. Answers with OTLP partial success when some spans were refused |
| `POST /v1/metrics`, `POST /v1/logs` | Accepted and not stored, so one exporter endpoint works for all three signals |
| `GET /v1/gate/snapshot` | `{agents: {name: {autonomy, tools, handoffs, high_risk}}, quarantine: {component: until}, fallbacks, faults}` |
| `POST /v1/gate/events` | `{events: [{agent, action, decision, reason}]}`. Report denials here; five in the short window suspend the agent |
| `POST /v1/approvals` | `{agent, action, detail}` → `{id, status: "pending"}` |
| `GET /v1/approvals/{id}` | `pending`, `approved`, `denied` or `expired` |

## For the dashboard (scope `read`)

| Route | What it returns |
|---|---|
| `GET /api/overview` | Vitals, every agent with its level and indicators, the what-uses-what edges, quarantines, running experiments, recent events |
| `GET /api/journeys?outcome=&agent=&session=&limit=` | Recent journeys |
| `GET /api/journeys/{trace_id}` | One journey: what it used per agent (pod, models, tools, cost) and every span |
| `GET /api/sessions` | Journeys grouped by user session |
| `GET /api/agents/{name}` | Indicators, policy, autonomy history, denied actions, journeys it broke |
| `GET /api/experiments` | Chaos experiments and their scores |
| `GET /api/incidents` | Incidents and whether their webhook was delivered |
| `GET /api/approvals?status=pending` | Go / no-go requests |
| `GET /api/rca` | What is breaking journeys right now, by cause, and the suspicion scores |
| `GET /metrics` | Prometheus text. Without a key: process health only |

## For operators (scope `operate`)

| Route | Body |
|---|---|
| `POST /api/agents/{name}/autonomy` | `{level: autonomous \| supervised \| suspended, reason}` |
| `POST /api/approvals/{id}/decision` | `{approve: true \| false, by}`. The first answer wins; a second gets 409 |
| `POST /api/experiments` | `{fault, target, intensity, duration_s, name}`. One running experiment per target |
| `POST /api/experiments/{id}/stop` | none |

## For administrators (scope `admin`)

| Route | Body |
|---|---|
| `PUT /api/fleet` | The fleet manifest: `{agents, prices, fallbacks}` (`docs/CONFIGURATION.md`) |
| `POST /api/keys` | `{scopes: [...], label}` → the new key, shown once |
| `DELETE /api/keys/{id}` | Revokes a key |
| `GET /api/audit` | Who changed what |

## No key needed

`GET /healthz` (the process is up), `GET /readyz` (the database answers; also says whether this replica leads),
and the dashboard's static files.
