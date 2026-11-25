"""The evaluator. One replica holds the lease and runs tick() on a timer. A tick:

1. judges journeys whose root span ended a moment ago (outcome, cost, root cause) and files their SLI events
2. names a culprit for silent failures once the statistics allow it
3. moves agents up or down the autonomy ladder
4. quarantines tools and models that keep causing failures, and releases them when their time is up
5. closes finished chaos experiments and scores them
6. sends incident webhooks

Ingest only appends spans, so any number of API replicas can take traffic while one evaluator does the thinking.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import uuid
from collections import defaultdict

import httpx
import sqlalchemy as sa
from sqlalchemy import func, insert, select, update

from themis import autonomy, chaos, rca, slo
from themis.db import (
    agents,
    approvals,
    audit_log,
    autonomy_events,
    experiments,
    gate_events,
    incidents,
    insert_ignore,
    journeys,
    quarantine,
    sli_buckets,
    spans,
    take_lease,
    tenants,
)
from themis.security import check_webhook_url, sign
from themis.settings import Settings

log = logging.getLogger("themis.engine")


class Superseded(Exception):
    """This evaluator lost a race with another one. Its transaction is rolled back and it tries again next tick."""


BATCH = 400
WINDOWS = sorted({w for _, long, short, _ in slo.BURN_RULES for w in (long, short)} | {slo.PERIOD})
QUARANTINE_MIN_BAD, QUARANTINE_RATIO, QUARANTINE_BASE, QUARANTINE_MAX = 5, 0.2, 600, 3600


def tick(engine: sa.Engine, s: Settings, now: float | None = None) -> dict:
    now = now or time.time()
    stats = {"journeys": 0, "moves": 0}
    with engine.connect() as conn:
        tenant_rows = conn.execute(select(tenants.c.id, tenants.c.config)).all()
    for tid, cfg in tenant_rows:
        for _ in range(10):  # drain a backlog, but never spin for ever inside one tick
            try:
                with engine.begin() as conn:
                    judged = finalise(conn, s, tid, cfg or {}, now)
            except Superseded:
                return stats  # the other evaluator is doing this tick's work
            stats["journeys"] += judged
            if judged < BATCH:
                break
        with engine.begin() as conn:
            recent = recent_journeys(conn, s, tid, now)
            reattribute(conn, tid, recent)
            stats["moves"] += evaluate_autonomy(conn, s, tid, now)
            evaluate_quarantine(conn, s, tid, now, recent)
            run_experiments(conn, s, tid, now)
    notify(engine, s)
    return stats


# ---------------------------------------------------------------- 1. journeys


def finalise(conn: sa.Connection, s: Settings, tid: str, cfg: dict, now: float) -> int:
    c = spans.c
    ready = select(c.id, c.trace_id).where(c.tenant_id == tid, c.is_root, ~c.done, c.end < now - s.settle_seconds)
    roots = conn.execute(ready.order_by(c.end).limit(BATCH)).all()
    if not roots:
        return 0
    # Claim the roots before doing anything with them. If a second evaluator is running (a leader that was slow
    # to notice it lost the lease), only one of the two claims succeeds, so no journey is ever counted twice.
    claim = update(spans).where(c.id.in_([r[0] for r in roots]), ~c.done).values(done=True)
    if conn.execute(claim).rowcount != len(roots):
        raise Superseded("another evaluator judged these journeys first")
    trace_ids = list(dict.fromkeys(r[1] for r in roots))
    # a trace judged on an earlier tick can show up again when a second root span arrives late: leave it alone
    j = journeys.c
    seen_before = set(conn.execute(select(j.trace_id).where(j.tenant_id == tid, j.trace_id.in_(trace_ids))).scalars())
    trace_ids = [t for t in trace_ids if t not in seen_before]
    if not trace_ids:
        return len(roots)
    by_trace = defaultdict(list)
    for row in conn.execute(select(spans).where(c.tenant_id == tid, c.trace_id.in_(trace_ids))).mappings():
        by_trace[row["trace_id"]].append(dict(row))
    agent_cfg = {n: cf or {} for n, cf in conn.execute(select(agents.c.name, agents.c.config).where(agents.c.tenant_id == tid))}
    buckets: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    fixes, rows, seen = [], [], {}
    for trace_id in trace_ids:
        ss = by_trace[trace_id]
        root = min((x for x in ss if x["is_root"]), key=lambda x: x["start"])
        fixes += _resolve(ss, cfg.get("prices") or {})
        rows.append(_judge(tid, root, ss))
        _account(ss, agent_cfg, s.bucket_seconds, buckets)
        for x in ss:
            if x["kind"] == "agent" and x["agent"]:
                seen[x["agent"]] = max(seen.get(x["agent"], 0.0), x["end"])
    conn.execute(insert(journeys), rows)
    if fixes:
        fix = update(spans).where(c.id == sa.bindparam("b_id")).values(agent=sa.bindparam("b_agent"), cost=sa.bindparam("b_cost"))
        conn.execute(fix, fixes)
    b = sli_buckets.c
    for (subject, sli, bucket), (good, total) in buckets.items():
        key = sa.and_(b.tenant_id == tid, b.subject == subject, b.sli == sli, b.bucket == bucket)
        if not conn.execute(update(sli_buckets).where(key).values(good=b.good + good, total=b.total + total)).rowcount:
            conn.execute(
                insert(sli_buckets).values(tenant_id=tid, subject=subject, sli=sli, bucket=bucket, good=good, total=total)
            )
    known = set(agent_cfg)
    insert_ignore(
        conn,
        agents,
        [{"tenant_id": tid, "name": n, "autonomy": "autonomous", "since": now, "config": {}} for n in seen if n not in known],
    )
    for name, at in seen.items():
        conn.execute(update(agents).where(agents.c.tenant_id == tid, agents.c.name == name).values(last_seen=at))
    return len(roots)


def _resolve(ss: list[dict], prices: dict) -> list[dict]:
    """Fill in what a span did not say itself: which agent it belongs to, and what a model call cost."""
    by_id = {x["span_id"]: x for x in ss}
    fixes = []
    for x in ss:
        changed = False
        if not x["agent"] and x["kind"] != "agent":
            parent, hops = by_id.get(x["parent_id"]), 0
            while parent and hops < 50:  # a cycle in bad telemetry must not hang the evaluator
                if parent["kind"] == "agent" and parent["agent"]:
                    x["agent"], changed = parent["agent"], True
                    break
                parent, hops = by_id.get(parent["parent_id"]), hops + 1
        if x["cost"] is None and x["kind"] == "model" and x["target"] in prices:
            per_in, per_out = prices[x["target"]]
            x["cost"], changed = (x["input_tokens"] * per_in + x["output_tokens"] * per_out) / 1e6, True
        if changed:
            fixes.append({"b_id": x["id"], "b_agent": x["agent"], "b_cost": x["cost"]})
    return fixes


def _judge(tid: str, root: dict, ss: list[dict]) -> dict:
    attrs = root.get("attrs") or {}
    outcome = attrs.get("themis.journey.outcome")
    if outcome not in ("success", "failed", "blocked"):
        outcome = "success" if root["ok"] else "failed"
    culprit = source = reason = confidence = None
    if outcome != "success":
        if by := attrs.get("themis.blocked.by"):
            culprit, source, confidence = str(by), "evidence", 1.0
            reason = f"{by} is suspended, so the journey was stopped before it could do harm"
        elif found := rca.attribute(ss):
            (culprit, reason, confidence), source = found, "evidence"
    return {
        "tenant_id": tid,
        "trace_id": root["trace_id"],
        "name": str(attrs.get("themis.journey.name") or root["name"])[:200],
        "session_id": _s(attrs.get("session.id") or attrs.get("gen_ai.conversation.id")),
        "user_id": _s(attrs.get("user.id")),
        "start": root["start"],
        "end": max(x["end"] for x in ss),
        "outcome": outcome,
        "cost": sum(x["cost"] or 0.0 for x in ss),
        "tokens": sum(x["input_tokens"] + x["output_tokens"] for x in ss),
        "span_count": len(ss),
        "components": sorted({comp for x in ss if (comp := rca.component(x))}),
        "culprit": culprit[:200] if culprit else None,
        "culprit_source": source,
        "culprit_reason": reason[:300] if reason else None,
        "confidence": confidence,
        "truth": _s(attrs.get("sim.truth"), 200),
    }


def _s(value, limit: int = 120) -> str | None:
    return str(value)[:limit] if value else None


def _account(ss: list[dict], agent_cfg: dict, bucket_seconds: float, buckets: dict) -> None:
    """Turn one journey's spans into good/total events per agent, SLI and time bucket."""
    model_cost = defaultdict(float)
    for x in ss:
        if x["kind"] == "model" and x["parent_id"]:
            model_cost[x["parent_id"]] += x["cost"] or 0.0

    def add(agent: str, sli: str, at: float, good: bool) -> None:
        cell = buckets[(agent, sli, int(at // bucket_seconds))]
        cell[0] += bool(good)
        cell[1] += 1

    for x in ss:
        agent, attrs = x["agent"], x.get("attrs") or {}
        if not agent:
            continue
        cfg = agent_cfg.get(agent) or {}
        if x["kind"] == "agent":
            add(agent, "task_success", x["end"], x["ok"])
            if ceiling := cfg.get("cost_ceiling"):
                add(agent, "cost", x["end"], model_cost[x["span_id"]] <= ceiling)
        elif x["kind"] == "handoff":
            add(agent, "handoff_success", x["end"], x["ok"] and attrs.get("themis.handoff.valid") is not False)
        elif x["kind"] == "tool" and x["error_type"] != "approval_denied":  # a denied action never ran
            high = attrs.get("themis.action.risk") == "high" or x["target"] in (cfg.get("high_risk") or ())
            if high:
                add(agent, "safe_actions", x["end"], x["ok"] and attrs.get("themis.check.passed") is not False)


def recent_journeys(conn: sa.Connection, s: Settings, tid: str, now: float) -> list[dict]:
    # shortcut: the last hour is scored in memory and capped at 20k journeys; keep counters per component beyond that
    j = journeys.c
    q = select(j.trace_id, j.end, j.outcome, j.culprit, j.culprit_source, j.components)
    q = q.where(j.tenant_id == tid, j.end >= now - s.scaled(slo.FAST_LONG)).order_by(j.end.desc()).limit(20_000)
    return [dict(r) for r in conn.execute(q).mappings()]


# ---------------------------------------------------------------- 2. silent failures


def suspicion_scores(recent: list[dict]) -> dict[str, float]:
    """Only failures with no evidence go into the statistics; explained failures would point at the wrong thing."""
    sample = [
        (r["outcome"] == "failed", r["components"])
        for r in recent
        if r["outcome"] == "success" or (r["outcome"] == "failed" and r["culprit_source"] != "evidence")
    ]
    return rca.suspicion(sample)


def reattribute(conn: sa.Connection, tid: str, recent: list[dict]) -> None:
    """Name a culprit for failures that left no evidence. A verdict from statistics is provisional: the first few
    failures can point at a bystander, so it is looked at again on every tick while the journey is in the window."""
    scores = suspicion_scores(recent)
    for r in recent:
        open_case = r["outcome"] == "failed" and r["culprit_source"] != "evidence"
        if open_case and (found := rca.pick(r["components"], scores)) and found[0] != r["culprit"]:
            r["culprit"], reason, confidence = found
            r["culprit_source"] = "statistics"
            where = sa.and_(journeys.c.tenant_id == tid, journeys.c.trace_id == r["trace_id"])
            values = dict(culprit=r["culprit"], culprit_source="statistics", culprit_reason=reason[:300], confidence=confidence)
            conn.execute(update(journeys).where(where).values(**values))


# ---------------------------------------------------------------- 3. autonomy


def window_counts(conn: sa.Connection, s: Settings, tid: str, now: float) -> dict:
    out: dict = defaultdict(lambda: defaultdict(dict))
    b = sli_buckets.c
    for w in WINDOWS:
        first = int((now - s.scaled(w)) // s.bucket_seconds)
        q = select(b.subject, b.sli, func.sum(b.good), func.sum(b.total)).where(b.tenant_id == tid, b.bucket >= first)
        for subject, sli, good, total in conn.execute(q.group_by(b.subject, b.sli)):
            out[subject][sli][w] = (int(good), int(total))
    return out


def agent_statuses(conn: sa.Connection, s: Settings, tid: str, now: float) -> dict[str, tuple[dict, list[slo.SliStatus]]]:
    counts = window_counts(conn, s, tid, now)
    out = {}
    for a in conn.execute(select(agents).where(agents.c.tenant_id == tid).order_by(agents.c.name)).mappings():
        targets = {**slo.DEFAULT_SLOS, **((a["config"] or {}).get("slo") or {})}
        mine = counts.get(a["name"], {})
        out[a["name"]] = (dict(a), [slo.evaluate(name, target, mine.get(name, {})) for name, target in targets.items()])
    return out


def evaluate_autonomy(conn: sa.Connection, s: Settings, tid: str, now: float) -> int:
    g = gate_events.c
    recent = sa.and_(g.tenant_id == tid, g.decision == "deny", g.at >= now - s.scaled(slo.FAST_SHORT))
    violations = {agent: n for agent, n in conn.execute(select(g.agent, func.count()).where(recent).group_by(g.agent))}
    moves = 0
    for name, (a, statuses) in agent_statuses(conn, s, tid, now).items():
        in_level = (now - (a["since"] or now)) * s.window_scale
        if move := autonomy.decide(a["autonomy"], in_level, statuses, violations.get(name, 0)):
            set_autonomy(conn, tid, name, a["autonomy"], move.level, move.reason, "themis", now)
            moves += 1
    return moves


def set_autonomy(conn: sa.Connection, tid: str, agent: str, old: str, new: str, reason: str, actor: str, now: float) -> None:
    if old == new:
        return
    where = sa.and_(agents.c.tenant_id == tid, agents.c.name == agent)
    conn.execute(update(agents).where(where).values(autonomy=new, reason=reason[:300], since=now))
    event = dict(tenant_id=tid, agent=agent, old=old, new=new, reason=reason[:300], actor=actor, at=now)
    conn.execute(insert(autonomy_events).values(**event))
    subject = f"agent:{agent}"
    if new == "autonomous":
        resolve_incidents(conn, tid, subject, now)
    else:
        severity = "critical" if new == "suspended" else "warning"
        detail = {"agent": agent, "from": old, "to": new, "reason": reason, "actor": actor}
        open_incident(conn, tid, f"agent_{new}", subject, severity, f"{agent} is now {new}: {reason}", detail, now)


def open_incident(conn, tid: str, kind: str, subject: str, severity: str, title: str, detail: dict, now: float) -> None:
    """One open incident per subject. A second problem on the same subject updates it and notifies again."""
    i = incidents.c
    values = dict(kind=kind, severity=severity, title=title[:300], detail=detail, notify_state="pending", notify_attempts=0)
    mine = sa.and_(i.tenant_id == tid, i.subject == subject, i.status == "open")
    if not conn.execute(update(incidents).where(mine).values(**values)).rowcount:
        row = dict(id=uuid.uuid4().hex, tenant_id=tid, subject=subject, status="open", opened_at=now, **values)
        conn.execute(insert(incidents).values(**row))


def resolve_incidents(conn, tid: str, subject: str, now: float) -> None:
    i = incidents.c
    mine = sa.and_(i.tenant_id == tid, i.subject == subject, i.status == "open")
    conn.execute(
        update(incidents).where(mine).values(status="resolved", resolved_at=now, notify_state="pending", notify_attempts=0)
    )


# ---------------------------------------------------------------- 4. quarantine


def evaluate_quarantine(conn: sa.Connection, s: Settings, tid: str, now: float, recent: list[dict]) -> None:
    """Agents get demoted; the tools and models they depend on get quarantined, so callers switch to a fallback."""
    q = quarantine.c
    rows = {r["component"]: dict(r) for r in conn.execute(select(quarantine).where(q.tenant_id == tid)).mappings()}
    for comp, r in rows.items():
        if r["active"] and r["until"] <= now:
            mine = sa.and_(q.tenant_id == tid, q.component == comp)
            conn.execute(update(quarantine).where(mine).values(active=False, released_at=now))
            r.update(active=False, released_at=now)
            resolve_incidents(conn, tid, comp, now)
    used, bad = defaultdict(int), defaultdict(int)
    horizon = now - s.scaled(slo.FAST_SHORT)  # act on what is failing now, not on the whole last hour
    for j in recent:
        for comp in j["components"] or ():
            if comp.startswith(("tool:", "model:")):
                # evidence from before the last release has been acted on already; only fresh failures count
                if j["end"] >= max(horizon, (rows.get(comp) or {}).get("released_at") or 0.0):
                    used[comp] += 1
                    bad[comp] += j["outcome"] == "failed" and j["culprit"] == comp
    for comp, n in bad.items():
        prev = rows.get(comp)
        if n < QUARANTINE_MIN_BAD or n / used[comp] < QUARANTINE_RATIO or (prev and prev["active"]):
            continue
        repeat = prev and now - (prev["released_at"] or 0.0) < s.scaled(QUARANTINE_MAX)
        strikes = (prev["strikes"] + 1) if repeat else 1
        ttl = s.scaled(min(QUARANTINE_BASE * 2 ** (strikes - 1), QUARANTINE_MAX))  # back off like a circuit breaker
        reason = f"named as the cause of {n} of the last {used[comp]} journeys that used it ({n / used[comp]:.0%})"
        values = dict(active=True, since=now, until=now + ttl, strikes=strikes, reason=reason)
        if prev:
            conn.execute(update(quarantine).where(q.tenant_id == tid, q.component == comp).values(**values))
        else:
            conn.execute(insert(quarantine).values(tenant_id=tid, component=comp, **values))
        detail = {"component": comp, "reason": reason, "until": now + ttl, "strikes": strikes}
        open_incident(conn, tid, "dependency_quarantined", comp, "warning", f"{comp} quarantined: {reason}", detail, now)


# ---------------------------------------------------------------- 5. chaos experiments


def run_experiments(conn: sa.Connection, s: Settings, tid: str, now: float) -> None:
    x = experiments.c
    for row in conn.execute(select(experiments).where(x.tenant_id == tid, x.status != "done")).mappings().all():
        if row["status"] == "running" and now >= row["started_at"] + row["duration_s"]:
            conn.execute(update(experiments).where(x.id == row["id"]).values(status="analysing", ended_at=now))
        elif row["status"] == "analysing" and now >= row["ended_at"] + s.settle_seconds + 2 * s.eval_interval + 1:
            result = chaos.analyse(conn, s, tid, dict(row))
            conn.execute(update(experiments).where(x.id == row["id"]).values(status="done", result=result))


# ---------------------------------------------------------------- 6. incident webhooks


def alert_payload(inc: dict, s: Settings) -> dict:
    """Alertmanager webhook format, so Aegis War Room, Argus, Incident Commander and Alertmanager tooling all read it."""

    def iso(t: float | None) -> str:
        return dt.datetime.fromtimestamp(t, dt.UTC).isoformat() if t else "0001-01-01T00:00:00Z"

    name = inc["subject"].split(":", 1)[-1]
    alert = {
        "status": "firing" if inc["status"] == "open" else "resolved",
        "labels": {
            "alertname": "Themis" + "".join(p.title() for p in inc["kind"].split("_")),
            "severity": inc["severity"],
            "service": name,
            "component": name,
            "themis_subject": inc["subject"],
            "source": "themis",
        },
        "annotations": {"summary": inc["title"], "description": json.dumps(inc["detail"], default=str)[:1500]},
        "startsAt": iso(inc["opened_at"]),
        "endsAt": iso(inc["resolved_at"]),
        "generatorURL": f"{s.external_url}/#/incidents",
        "fingerprint": inc["id"],
    }
    return {
        "version": "4",
        "groupKey": inc["id"],
        "status": alert["status"],
        "receiver": "themis",
        "groupLabels": {"alertname": alert["labels"]["alertname"]},
        "commonLabels": alert["labels"],
        "commonAnnotations": alert["annotations"],
        "externalURL": s.external_url,
        "alerts": [alert],
    }


def notify(engine: sa.Engine, s: Settings) -> None:
    """At-least-once delivery. The network call happens outside any database transaction."""
    i = incidents.c
    with engine.connect() as conn:
        pending = [dict(r) for r in conn.execute(select(incidents).where(i.notify_state == "pending").limit(20)).mappings()]
    for inc in pending:
        if not s.webhooks or inc["severity"] == "info":
            state = "skipped"
        else:
            body = json.dumps(alert_payload(inc, s)).encode()
            delivered = [_post(s, url, body) for url in s.webhooks]
            state = "sent" if all(delivered) else "failed" if inc["notify_attempts"] >= 4 else "pending"
        # if the incident changed while we were sending, leave it pending so the newer state goes out too
        unchanged = sa.and_(i.id == inc["id"], i.status == inc["status"], i.title == inc["title"])
        with engine.begin() as conn:
            conn.execute(update(incidents).where(unchanged).values(notify_state=state, notify_attempts=i.notify_attempts + 1))


def _post(s: Settings, url: str, body: bytes) -> bool:
    headers = {"Content-Type": "application/json", "User-Agent": "themis"}
    if s.webhook_token:
        headers["Authorization"] = f"Bearer {s.webhook_token}"
    if s.webhook_secret:
        headers["X-Themis-Signature"] = sign(s.webhook_secret, body)
    try:
        check_webhook_url(url, s.webhook_allow_private)
        return httpx.post(url, content=body, headers=headers, timeout=3.0, follow_redirects=False).is_success
    except (httpx.HTTPError, ValueError) as e:
        log.warning("webhook to %s failed: %s", url.split("?")[0], type(e).__name__)
        return False


# ---------------------------------------------------------------- housekeeping


def prune(engine: sa.Engine, s: Settings, now: float) -> None:
    old = now - s.retention_hours * 3600
    with engine.begin() as conn:
        conn.execute(sa.delete(spans).where(spans.c.end < old))
        conn.execute(sa.delete(journeys).where(journeys.c.end < old))
        conn.execute(sa.delete(gate_events).where(gate_events.c.at < old))
        conn.execute(sa.delete(approvals).where(approvals.c.requested_at < old))
        conn.execute(sa.delete(audit_log).where(audit_log.c.at < now - 90 * 86400))
        first = int((now - s.scaled(slo.PERIOD)) // s.bucket_seconds)
        conn.execute(sa.delete(sli_buckets).where(sli_buckets.c.bucket < first))


class Evaluator(threading.Thread):
    """Runs tick() on a timer in whichever replica holds the lease. The others stand by and take over if it dies."""

    def __init__(self, engine: sa.Engine, s: Settings):
        super().__init__(daemon=True, name="themis-evaluator")
        self.engine, self.s = engine, s
        self.stop = threading.Event()
        self.leader, self.ticks, self.last_tick = False, 0, 0.0

    def run(self) -> None:
        while not self.stop.wait(self.s.eval_interval):
            try:
                now = time.time()
                with self.engine.begin() as conn:
                    self.leader = take_lease(conn, "evaluator", self.s.instance, max(3 * self.s.eval_interval, 5.0), now)
                if self.leader:
                    tick(self.engine, self.s, now)
                    self.ticks, self.last_tick = self.ticks + 1, now
                    if self.ticks % 200 == 1:
                        prune(self.engine, self.s, now)
            except Exception:  # noqa: BLE001 - the loop must survive any single bad tick; the error is logged with its trace
                log.exception("evaluator tick failed")
