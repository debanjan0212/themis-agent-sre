"""The control loop, with the clock in the test's hands.

Spans are written straight to the database and tick() is called with an explicit "now", so each test walks
through hours of production time in milliseconds. Windows are the real ones: 1h / 5m, 14.4x.
"""

import dataclasses
import json
import time

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from tests.conftest import T0, Receiver, Trace, simple
from themis import db, security
from themis import engine as ev


def agent_row(conn, name="coder", tenant="default", **config):
    conn.execute(
        sa.insert(db.agents).values(tenant_id=tenant, name=name, autonomy="autonomous", since=T0 - 10_000, config=config)
    )


def level(engine, name="coder", tenant="default"):
    with engine.connect() as conn:
        return conn.execute(
            select(db.agents.c.autonomy).where(db.agents.c.name == name, db.agents.c.tenant_id == tenant)
        ).scalar()


def rows(engine, table, *where):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(table).where(*where)).mappings()]


def buckets(engine, subject):
    out = {}
    for r in rows(engine, db.sli_buckets, db.sli_buckets.c.subject == subject):
        good, total = out.get(r["sli"], (0, 0))
        out[r["sli"]] = (good + r["good"], total + r["total"])
    return out


# ---------------------------------------------------------------- judging a journey


def test_a_journey_is_judged_once_with_cost_owner_and_components(engine, settings):
    with engine.begin() as conn:
        conn.execute(sa.update(db.tenants).values(config={"prices": {"sim-large": [3.0, 15.0]}}))
        t = Trace(T0, name="ship_change")
        a = t.agent("coder")
        t.model(a, None, "sim-large", tokens=(1_000_000, 0))  # the SDK did not say which agent made the call
        t.tool(a, None, "repo.write")
        t.handoff(a, "coder", "reviewer")
        t.save(conn)
    assert ev.tick(engine, settings, T0 + 1)["journeys"] == 1
    assert ev.tick(engine, settings, T0 + 2)["journeys"] == 0  # never judged twice
    (j,) = rows(engine, db.journeys)
    assert (j["name"], j["outcome"], j["cost"], j["tokens"], j["session_id"]) == ("ship_change", "success", 3.0, 1_000_000, "s1")
    assert j["components"] == ["agent:coder", "handoff:coder->reviewer", "model:sim-large", "tool:repo.write"]
    owners = {r["kind"]: r["agent"] for r in rows(engine, db.spans, db.spans.c.kind.in_(["model", "tool"]))}
    assert owners == {"model": "coder", "tool": "coder"}  # filled in from the parent agent span
    assert buckets(engine, "coder") == {"task_success": (1, 1), "handoff_success": (1, 1)}
    assert level(engine) == "autonomous"  # an agent Themis has never heard of is registered, trusted by default
    uses = {(r["kind"], r["target"]): (r["calls"], r["errors"], round(r["cost"], 6)) for r in rows(engine, db.edge_buckets)}
    assert uses == {
        ("agent", ""): (1, 0, 0.0),
        ("model", "sim-large"): (1, 0, 3.0),
        ("tool", "repo.write"): (1, 0, 0.0),
        ("handoff", "reviewer"): (1, 0, 0.0),
    }


def test_two_evaluators_cannot_count_the_same_journey_twice(engine, settings):
    """A leader that was slow to notice it lost the lease may still be mid-tick. Only one claim on a journey wins."""
    with engine.begin() as conn:
        simple(conn, T0)
    with engine.connect() as slow:  # the old leader reads the ready journey...
        ready = slow.execute(select(db.spans.c.id).where(db.spans.c.is_root, ~db.spans.c.done)).scalars().all()
        slow.rollback()
    assert ev.tick(engine, settings, T0 + 1)["journeys"] == 1  # ...the new leader judges it...
    with engine.begin() as conn:  # ...and the old leader's claim on the same rows now gets nothing
        claimed = conn.execute(sa.update(db.spans).where(db.spans.c.id.in_(ready), ~db.spans.c.done).values(done=True)).rowcount
    assert claimed == 0
    assert buckets(engine, "coder") == {"task_success": (1, 1)}


def test_a_late_second_root_span_does_not_judge_the_journey_again(engine, settings):
    with engine.begin() as conn:
        t = simple(conn, T0)
    ev.tick(engine, settings, T0 + 1)
    with engine.begin() as conn:  # a stray span with no parent, same trace, arrives a minute later
        late = dict(t.rows[0], span_id="ffffffffffffffff", done=False, start=T0 + 60, end=T0 + 61)
        conn.execute(sa.insert(db.spans), [late])
    ev.tick(engine, settings, T0 + 70)
    assert len(rows(engine, db.journeys)) == 1 and buckets(engine, "coder") == {"task_success": (1, 1)}
    assert all(r["done"] for r in rows(engine, db.spans, db.spans.c.is_root))


def test_one_trace_that_cannot_be_judged_does_not_stop_the_others(engine, settings, monkeypatch):
    with engine.begin() as conn:
        good, bad = simple(conn, T0), simple(conn, T0)
    real = ev._judge

    def judge(tid, root, ss):
        if root["trace_id"] == bad.trace_id:
            raise ValueError("an attribute nobody expected")
        return real(tid, root, ss)

    monkeypatch.setattr(ev, "_judge", judge)
    before = ev.COUNTERS["traces_skipped"]
    assert ev.tick(engine, settings, T0 + 1)["journeys"] == 2  # both claimed...
    assert [j["trace_id"] for j in rows(engine, db.journeys)] == [good.trace_id]  # ...the good one judged
    assert ev.COUNTERS["traces_skipped"] == before + 1
    assert buckets(engine, "coder") == {"task_success": (1, 1)}  # the skipped trace counted for nothing
    assert ev.tick(engine, settings, T0 + 2)["journeys"] == 0  # and it is not retried for ever


def test_the_tick_reports_how_long_journeys_waited_to_be_judged(engine, settings):
    with engine.begin() as conn:
        simple(conn, T0 - 40)
        simple(conn, T0 - 5)
    assert ev.tick(engine, settings, T0)["lag"] == pytest.approx(40.0)
    assert ev.tick(engine, settings, T0 + 1)["lag"] == 0.0  # nothing left waiting


def test_cost_and_safety_indicators_follow_the_fleet_manifest(engine, settings):
    with engine.begin() as conn:
        conn.execute(sa.update(db.tenants).values(config={"prices": {"sim-small": [1.0, 1.0]}}))
        agent_row(conn, "deployer", cost_ceiling=0.001, high_risk=["deploy.apply"])
        for tokens, tool_kw in (
            ((500, 0), {}),  # cheap, safe
            ((2000, 0), {"themis.check.passed": False}),  # over the ceiling, and the action failed its check
            ((500, 0), {"ok": False, "error": "approval_denied"}),  # a human said no: the action never ran
        ):
            t = Trace(T0)
            a = t.agent("deployer")
            t.model(a, "deployer", "sim-small", tokens=tokens)
            t.tool(a, "deployer", "deploy.apply", **tool_kw)  # high risk because the manifest says so, not the span
            t.save(conn)
    ev.tick(engine, settings, T0 + 1)
    assert buckets(engine, "deployer") == {"task_success": (3, 3), "cost": (2, 3), "safe_actions": (1, 2)}


def test_a_journey_stopped_by_a_suspension_names_the_agent_and_costs_nobody_budget(engine, settings):
    with engine.begin() as conn:
        Trace(T0, outcome="blocked", **{"themis.blocked.by": "agent:coder"}).save(conn)
    ev.tick(engine, settings, T0 + 1)
    (j,) = rows(engine, db.journeys)
    assert (j["outcome"], j["culprit"], j["culprit_source"], j["confidence"]) == ("blocked", "agent:coder", "evidence", 1.0)
    assert rows(engine, db.sli_buckets) == []


# ---------------------------------------------------------------- the autonomy ladder, end to end


def test_burn_demotes_persistence_suspends_and_only_good_behaviour_restores(engine, settings):
    with engine.begin() as conn:
        for i in range(20):  # 12 of 20 tasks fail inside five minutes: 60% errors against a 3% budget is 20x
            simple(conn, T0 - 5 * i, ok=i >= 12)
    ev.tick(engine, settings, T0 + 1)
    assert level(engine) == "supervised"
    (incident,) = rows(engine, db.incidents)
    assert (incident["status"], incident["severity"], incident["subject"]) == ("open", "warning", "agent:coder")

    ev.tick(engine, settings, T0 + 300)
    assert level(engine) == "supervised"  # inside the grace period nothing more happens

    with engine.begin() as conn:
        for i in range(15):  # still failing ten minutes later
            simple(conn, T0 + 500 + 6 * i, ok=False)
    ev.tick(engine, settings, T0 + 602)
    assert level(engine) == "suspended"
    (incident,) = rows(engine, db.incidents)  # the same incident, escalated
    assert incident["severity"] == "critical" and "still burning" in incident["title"]

    with engine.begin() as conn:  # good traffic does not un-suspend: only a person does
        for i in range(30):
            simple(conn, T0 + 9000 + i)
    ev.tick(engine, settings, T0 + 9100)
    assert level(engine) == "suspended"

    with engine.begin() as conn:
        ev.set_autonomy(conn, "default", "coder", "suspended", "supervised", "fault fixed", "human:debanjan", T0 + 9200)
        for i in range(30):
            simple(conn, T0 + 9300 + i)
    ev.tick(engine, settings, T0 + 9400)
    assert level(engine) == "supervised"  # calm, but the cool-down has not passed
    with engine.begin() as conn:
        for i in range(30):
            simple(conn, T0 + 11_000 + i)
    ev.tick(engine, settings, T0 + 11_100)
    assert level(engine) == "autonomous"
    assert [i["status"] for i in rows(engine, db.incidents)] == ["resolved"]
    moves = [(e["old"], e["new"], e["actor"]) for e in rows(engine, db.autonomy_events)]
    assert moves == [
        ("autonomous", "supervised", "themis"),
        ("supervised", "suspended", "themis"),
        ("suspended", "supervised", "human:debanjan"),
        ("supervised", "autonomous", "themis"),
    ]


def test_policy_violations_suspend_without_waiting_for_a_window(engine, settings):
    with engine.begin() as conn:
        agent_row(conn)
        for _ in range(5):
            conn.execute(
                sa.insert(db.gate_events).values(
                    tenant_id="default", agent="coder", action="tool:secrets.read", decision="deny", at=T0
                )
            )
    ev.tick(engine, settings, T0 + 1)
    assert level(engine) == "suspended"
    assert "policy violations" in rows(engine, db.incidents)[0]["title"]


def test_tenants_with_the_same_agent_name_are_judged_apart(engine, settings):
    with engine.begin() as conn:
        db.insert_ignore(conn, db.tenants, {"id": "other", "name": "other", "config": {}})
        for i in range(20):
            simple(conn, T0 - i)  # ours: healthy
            t = Trace(T0 - i, tenant="other", outcome="failed")  # theirs: failing
            t.agent("coder", ok=False, error="task_error")
            t.save(conn)
    ev.tick(engine, settings, T0 + 1)
    assert (level(engine, tenant="default"), level(engine, tenant="other")) == ("autonomous", "supervised")


# ---------------------------------------------------------------- dependencies: quarantine and silent failures


def model_outage(conn, at, failed):
    for i in range(16):
        bad = i < failed
        t = Trace(at - i, outcome="failed" if bad else None)
        a = t.agent("planner", ok=not bad, error="model_unavailable" if bad else None)
        t.model(a, "planner", "sim-large", ok=not bad)
        t.save(conn)


def test_a_failing_model_is_quarantined_released_and_backed_off(engine, settings):
    s = dataclasses.replace(settings, eval_interval=1.0)
    with engine.begin() as conn:
        x = dict(id="x1", tenant_id="default", name="429s", fault="model_rate_limit", target="model:sim-large", intensity=0.8)
        conn.execute(sa.insert(db.experiments).values(**x, duration_s=730, status="running", started_at=T0 - 20))
        model_outage(conn, T0, failed=6)
    ev.tick(engine, settings, T0 + 1)
    assert {j["culprit"] for j in rows(engine, db.journeys) if j["outcome"] == "failed"} == {"model:sim-large"}
    (q,) = rows(engine, db.quarantine)
    assert q["active"] and q["strikes"] == 1 and q["until"] - q["since"] == 600
    assert rows(engine, db.incidents, db.incidents.c.subject == "model:sim-large")[0]["status"] == "open"

    ev.tick(engine, settings, T0 + 602)  # time is up: let traffic try the model again
    (q,) = rows(engine, db.quarantine)
    assert not q["active"]
    assert rows(engine, db.incidents, db.incidents.c.subject == "model:sim-large")[0]["status"] == "resolved"
    ev.tick(engine, settings, T0 + 650)
    assert not rows(engine, db.quarantine)[0]["active"]  # the old failures are not counted a second time

    with engine.begin() as conn:
        model_outage(conn, T0 + 700, failed=6)  # it is still broken
    ev.tick(engine, settings, T0 + 701)
    (q,) = rows(engine, db.quarantine)
    assert q["active"] and q["strikes"] == 2 and q["until"] - q["since"] == 1200  # twice as long this time

    # a chaos experiment that ran through all of this is scored on the FIRST quarantine, not the latest one
    ev.tick(engine, s, T0 + 711)
    ev.tick(engine, s, T0 + 720)
    detection = rows(engine, db.experiments)[0]["result"]["detection"]
    assert detection == {"detected": True, "seconds": 21.0, "response": "dependency quarantined, callers on fallback"}


def test_a_silently_wrong_tool_is_found_by_statistics(engine, settings):
    """Every span is green. The only sign is that journeys using kb.search end badly more often."""
    with engine.begin() as conn:
        for i in range(30):
            bad = i < 12
            t = Trace(T0 - i, outcome="failed" if bad else None)
            t.tool(t.agent("planner"), "planner", "kb.search")
            t.save(conn)
            t = Trace(T0 - i)
            t.tool(t.agent("researcher"), "researcher", "web.fetch")
            t.save(conn)
    ev.tick(engine, settings, T0 + 1)
    failed = [j for j in rows(engine, db.journeys) if j["outcome"] == "failed"]
    assert len(failed) == 12
    assert {(j["culprit"], j["culprit_source"]) for j in failed} == {("tool:kb.search", "statistics")}
    assert rows(engine, db.quarantine)[0]["component"] == "tool:kb.search"
    assert level(engine, "planner") == "autonomous"  # the planner did nothing wrong, and is not punished for it


def test_a_verdict_from_statistics_is_revised_when_more_journeys_arrive(engine, settings):
    def batch(conn, at, n, tools, failed):
        for i in range(n):
            t = Trace(at - i, outcome="failed" if failed else None)
            a = t.agent("planner")
            for tool in tools:
                t.tool(a, "planner", tool)
            t.save(conn)

    with engine.begin() as conn:  # at first every failure also used lint.run, which looks guiltier than kb.search
        batch(conn, T0, 9, ["kb.search", "lint.run"], failed=True)
        batch(conn, T0, 9, ["kb.search"], failed=False)
        batch(conn, T0, 9, ["web.fetch"], failed=False)
    ev.tick(engine, settings, T0 + 1)
    assert {j["culprit"] for j in rows(engine, db.journeys) if j["outcome"] == "failed"} == {"tool:lint.run"}

    with engine.begin() as conn:  # then lint.run turns out to be fine on its own, and kb.search keeps failing
        batch(conn, T0 + 60, 30, ["lint.run"], failed=False)
        batch(conn, T0 + 60, 20, ["kb.search"], failed=True)
    ev.tick(engine, settings, T0 + 61)
    assert {j["culprit"] for j in rows(engine, db.journeys) if j["outcome"] == "failed"} == {"tool:kb.search"}


# ---------------------------------------------------------------- chaos scoring


def test_an_experiment_is_closed_and_scored_against_ground_truth(engine, settings):
    s = dataclasses.replace(settings, eval_interval=1.0)
    with engine.begin() as conn:
        x = dict(id="x1", tenant_id="default", name="poison", fault="context_poison", target="agent:coder", intensity=0.5)
        conn.execute(sa.insert(db.experiments).values(**x, duration_s=60, status="running", started_at=T0))
        for i in range(30):
            at = T0 + 2 + i * 1.5
            who = "coder" if i < 10 else "scribe" if i < 12 else None
            t = Trace(at, outcome="failed" if who else None, **({"sim.truth": f"agent:{who}"} if who else {}))
            t.agent(who or "coder", ok=not who, error="task_error" if who else None)
            t.save(conn)
    ev.tick(engine, s, T0 + 50)  # judges the journeys; the 10 failures demote coder, which is the "response"
    ev.tick(engine, s, T0 + 61)
    assert rows(engine, db.experiments)[0]["status"] == "analysing"
    ev.tick(engine, s, T0 + 70)
    (done,) = rows(engine, db.experiments)
    r = done["result"]
    assert done["status"] == "done" and r["journeys"]["total"] == 30 and r["journeys"]["failed"] == 12
    assert r["rca"] == {
        "fault_caused": 10, "named_target": 10, "by_evidence": 10, "by_statistics": 0, "named_other": 0, "unknown": 0,
        "all_labelled": 12, "all_correct": 12,
    }  # fmt: skip
    assert r["detection"] == {"detected": True, "seconds": 50.0, "response": "agent moved to supervised"}
    assert r["blast_radius"]["agents"] == ["coder", "scribe"]


# ---------------------------------------------------------------- incident webhooks


def test_incidents_go_out_as_signed_alertmanager_webhooks(engine, settings, receiver):
    secret = security.new_key()[1]
    s = dataclasses.replace(
        settings, webhooks=(receiver.url,), webhook_allow_private=True, webhook_secret=secret, webhook_token="tok"
    )
    with engine.begin() as conn:
        ev.open_incident(
            conn, "default", "agent_suspended", "agent:coder", "critical", "coder is now suspended", {"agent": "coder"}, T0
        )
    ev.notify(engine, s)
    ev.notify(engine, s)  # nothing new: must not send twice
    with engine.begin() as conn:
        ev.resolve_incidents(conn, "default", "agent:coder", T0 + 60)
    ev.notify(engine, s)
    assert len(receiver.got) == 2
    headers, body = receiver.got[0]
    assert headers["X-Themis-Signature"] == security.sign(secret, body) and headers["Authorization"] == "Bearer tok"
    firing, resolved = (json.loads(b) for _, b in receiver.got)
    alert = firing["alerts"][0]
    assert firing["version"] == "4" and alert["status"] == "firing" and resolved["alerts"][0]["status"] == "resolved"
    assert alert["labels"] == {
        "alertname": "ThemisAgentSuspended", "severity": "critical", "service": "coder", "component": "coder",
        "themis_subject": "agent:coder", "source": "themis",
    }  # fmt: skip
    assert alert["fingerprint"] == resolved["alerts"][0]["fingerprint"]  # receivers de-duplicate on this
    assert rows(engine, db.incidents)[0]["notify_state"] == "sent"


def test_webhooks_to_private_addresses_are_refused_unless_allowed(engine, settings, receiver):
    s = dataclasses.replace(settings, webhooks=(receiver.url,))  # loopback, and THEMIS_WEBHOOK_ALLOW_PRIVATE is off
    with engine.begin() as conn:
        ev.open_incident(conn, "default", "agent_suspended", "agent:coder", "critical", "t", {}, T0)
    for _ in range(5):
        ev.notify(engine, s)
    assert receiver.got == [] and rows(engine, db.incidents)[0]["notify_state"] == "failed"
    with pytest.raises(ValueError):
        security.check_webhook_url("http://169.254.169.254/latest/meta-data", allow_private=False)  # cloud metadata
    with pytest.raises(ValueError):
        security.check_webhook_url("file:///etc/passwd", allow_private=True)


def test_a_slow_receiver_cannot_hold_up_the_evaluator(engine, settings):
    slow = Receiver(delay=1.5)
    s = dataclasses.replace(settings, webhooks=(slow.url,), webhook_allow_private=True)
    with engine.begin() as conn:
        for i in range(5):
            ev.open_incident(conn, "default", "agent_suspended", f"agent:a{i}", "critical", "t", {}, T0)
    started = time.monotonic()
    ev.notify(engine, s, budget=1.0)
    took = time.monotonic() - started
    slow.server.shutdown()
    assert took < 3.5  # one delivery in flight when the budget ran out, not five
    states = sorted(i["notify_state"] for i in rows(engine, db.incidents))
    assert states.count("sent") == 1 and states.count("pending") == 4  # the rest go out on the next tick


def test_a_failing_receiver_is_retried_then_given_up_on(engine, settings):
    down = Receiver(status=500)
    s = dataclasses.replace(settings, webhooks=(down.url,), webhook_allow_private=True)
    with engine.begin() as conn:
        ev.open_incident(conn, "default", "agent_suspended", "agent:coder", "critical", "t", {}, T0)
    states = []
    for _ in range(6):
        ev.notify(engine, s)
        states.append(rows(engine, db.incidents)[0]["notify_state"])
    down.server.shutdown()
    assert states == ["pending"] * 4 + ["failed"] * 2 and len(down.got) == 5


# ---------------------------------------------------------------- housekeeping


def test_prune_drops_only_what_is_past_retention(engine, settings):
    with engine.begin() as conn:
        simple(conn, T0 - 100 * 3600)
        simple(conn, T0 - 60)
    ev.tick(engine, settings, T0)
    ev.prune(engine, settings, T0)
    assert len(rows(engine, db.journeys)) == 1 and {r["end"] > T0 - 3600 for r in rows(engine, db.spans)} == {True}
