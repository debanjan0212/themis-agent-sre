# Runbook

What to do when something happens. Each section starts with what you will see.

## An agent was put under supervision

**You see:** a `warning` incident, "‹agent› is now supervised: ‹indicator› is burning its budget …x too fast".

1. Open the agent's page. The indicator table shows which budget is burning.
2. Open "Journeys it broke". The cause column says whether it is the agent itself or something it depends on.
3. While it is supervised, its high-risk actions wait on the Incidents page for your go or no-go. Answer
   them quickly: the agent stops waiting after 30 seconds (the `Gate.approve` default) and takes that as a no.
4. Fix the cause. Once it has been supervised for 30 minutes and every indicator is under 1x, Themis gives
   autonomy back by itself.

## An agent was suspended

**You see:** a `critical` incident. Journeys that need the agent end as "stopped safely".

Either it kept burning for 10 minutes while supervised, or it broke policy five times in five minutes (it tried
tools outside its allow-list; the agent page lists the attempts).

1. Treat policy violations as a possible prompt injection: look at what the agent was reading.
2. When the cause is fixed, open the agent's page, write the reason and choose **Supervise**. Themis never
   un-suspends an agent on its own.
3. It returns to full autonomy after the cool-down, as above.

## A tool or model was quarantined

**You see:** a `warning` incident, "‹tool:x› quarantined: named as the cause of n of the last m journeys".

Runtimes are already using the fallback. The quarantine lifts by itself (10 minutes the first time) and
traffic tries the original again. If it still fails, it is quarantined again for twice as long, up to an hour.
Nothing to do in Themis; fix the dependency.

If it was quarantined "by statistics" and you believe it is innocent, check what else changed for the journeys
that use it: the statistics find what failing journeys have in common, which is strong evidence but not proof.

## The dashboard is stale, or nothing is being judged

`GET /readyz` on each replica shows `"leader": true` on exactly one. `GET /metrics` shows
`themis_evaluator_last_tick_age_seconds`.

- No leader: the database is unreachable or every evaluator is crashing. Check the pod logs for "evaluator tick
  failed"; the error and its trace are logged every time.
- Tick age growing on the leader: the evaluator is behind. Look at database load first.
- Agents keep working through this: they hold their last gate snapshot.

## The database is down

`/readyz` answers 503, so Kubernetes takes the pods out of the Service. Ingest fails and exporters retry and
then drop spans: a gap in the data, not an outage for your agents. When the database is back, a replica takes
the lease and judging resumes.

Back up Postgres like any other database. Losing it loses history and autonomy state; agents start as
autonomous again, so apply the fleet manifest and re-suspend anything that was suspended.

## Rotating a key

```bash
curl -X POST $THEMIS/api/keys -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"scopes": ["ingest"], "label": "agents-2027-01"}'
# roll the new key out to the agents, then:
curl -X DELETE $THEMIS/api/keys/<old key id> -H "Authorization: Bearer $ADMIN"
```

The key id is the middle part of a key: `thm_<id>_<secret>`. `GET /api/audit` shows who created and revoked what.

## Running a chaos experiment safely

Faults only reach runtimes that read them from the gate snapshot, and only in the tenant whose key started the
experiment. Start small (`--intensity 0.1`), keep it short, and run it in staging first. `POST
/api/experiments/<id>/stop` ends one early. The result is on the Chaos lab page a few seconds after it ends.
