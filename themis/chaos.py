"""Chaos experiments for agents: what can be injected, and how a finished experiment is scored.

Themis only announces a fault (in the gate snapshot). The agent runtime applies it at its own boundary, the same
way it applies autonomy and quarantine. The bundled world does this in themis/world/sim.py.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import select

from themis.db import autonomy_events, incidents, journeys, spans
from themis.settings import Settings

# fault -> the kinds of component it may target
FAULTS = {
    "model_rate_limit": ("model",),  # the model answers 429 some of the time
    "tool_wrong_result": ("tool",),  # the tool returns a wrong answer, often with no error at all
    "context_poison": ("agent",),  # injected instructions push the agent off its task
    "rogue_agent": ("agent",),  # the agent takes unsafe high-risk actions and burns tokens
    "latency": ("model", "tool"),  # the dependency slows down
}


def validate(fault: str, target: str, intensity: float, duration_s: float) -> None:
    if fault not in FAULTS:
        raise ValueError(f"unknown fault '{fault}'. Choose one of: {', '.join(FAULTS)}")
    kind, _, name = target.partition(":")
    if kind not in FAULTS[fault] or not name:
        raise ValueError(f"{fault} needs a target like '{FAULTS[fault][0]}:<name>'")
    if not 0 < intensity <= 1:
        raise ValueError("intensity is a probability: more than 0, at most 1")
    if not 1 <= duration_s <= 3600:
        raise ValueError("duration must be between 1 second and 1 hour")


def analyse(conn: sa.Connection, s: Settings, tid: str, x: dict) -> dict:
    """Blast radius, detection, containment and root-cause accuracy for one finished experiment."""
    start, end, target = x["started_at"], x["ended_at"], x["target"]
    j = journeys.c
    cols = (j.start, j.outcome, j.cost, j.components, j.culprit, j.culprit_source, j.truth, j.user_id, j.session_id, j.name)
    window = sa.and_(j.tenant_id == tid, j.start >= start - (end - start), j.start <= end)
    rows = [dict(r) for r in conn.execute(select(*cols).where(window)).mappings()]
    during = [r for r in rows if r["start"] >= start]
    before = [r for r in rows if r["start"] < start]

    # The first thing Themis itself did about the target after the fault began.
    kind, _, name = target.partition(":")
    if kind == "agent":
        e = autonomy_events.c
        acted = select(e.at, e.new).where(e.tenant_id == tid, e.agent == name, e.actor == "themis", e.at >= start)
        action = conn.execute(acted.order_by(e.at).limit(1)).first()
        detected_at, response = (action[0], f"agent moved to {action[1]}") if action else (None, None)
    else:
        # every quarantine opens an incident; the quarantine row itself only remembers the most recent one
        i = incidents.c
        first = sa.and_(i.tenant_id == tid, i.subject == target, i.kind == "dependency_quarantined", i.opened_at >= start)
        since = conn.execute(select(sa.func.min(i.opened_at)).where(first)).scalar()
        detected_at, response = (since, "dependency quarantined, callers on fallback") if since else (None, None)

    def caused(r: dict) -> bool:  # the simulator labels the true cause; real traffic falls back to "touched the target"
        return r["truth"] == target if r["truth"] else target in (r["components"] or ())

    hit = [r for r in during if r["outcome"] != "success" and caused(r)]
    labelled = [r for r in during if r["outcome"] != "success" and r["truth"]]
    cut = detected_at or end
    pre, post = [r for r in during if r["start"] < cut], [r for r in during if r["start"] >= cut]

    sp = spans.c
    risky = sa.and_(sp.tenant_id == tid, sp.kind == "tool", sp.end >= start, sp.end <= end + s.settle_seconds)
    unsafe = [
        at
        for at, attrs in conn.execute(select(sp.end, sp.attrs).where(risky))
        if (attrs or {}).get("themis.action.risk") == "high" and (attrs or {}).get("themis.check.passed") is False
    ]
    failed = [r for r in during if r["outcome"] == "failed"]
    return {
        "journeys": _summary(during),
        "baseline": _summary(before),
        "detection": {
            "detected": detected_at is not None,
            "seconds": round(detected_at - start, 2) if detected_at else None,
            "response": response,
        },
        "containment": {
            "before_response": _summary(pre),
            "after_response": _summary(post),
            "unsafe_actions_before": sum(1 for at in unsafe if at < cut),
            "unsafe_actions_after": sum(1 for at in unsafe if at >= cut),
        },
        "blast_radius": {
            "agents": sorted({c[6:] for r in failed for c in r["components"] or () if c.startswith("agent:")}),
            "journey_types": sorted({r["name"] for r in failed if r["name"]}),
            "users": len({r["user_id"] for r in failed if r["user_id"]}),
            "sessions": len({r["session_id"] for r in failed if r["session_id"]}),
        },
        "rca": {
            "fault_caused": len(hit),
            "named_target": sum(1 for r in hit if r["culprit"] == target),
            "by_evidence": sum(1 for r in hit if r["culprit"] == target and r["culprit_source"] == "evidence"),
            "by_statistics": sum(1 for r in hit if r["culprit"] == target and r["culprit_source"] == "statistics"),
            "named_other": sum(1 for r in hit if r["culprit"] not in (None, target)),
            "unknown": sum(1 for r in hit if r["culprit"] is None),
            "all_labelled": len(labelled),
            "all_correct": sum(1 for r in labelled if r["culprit"] == r["truth"]),
        },
    }


def _summary(rows: list[dict]) -> dict:
    n = len(rows)
    count = {o: sum(1 for r in rows if r["outcome"] == o) for o in ("success", "failed", "blocked")}
    return {
        "total": n,
        **count,
        "failed_rate": round(count["failed"] / n, 4) if n else None,
        "cost_per_journey": round(sum(r["cost"] or 0 for r in rows) / n, 6) if n else None,
    }
