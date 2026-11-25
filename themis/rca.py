"""Cross-agent root cause. Pure functions, no I/O.

When a journey fails, which agent, tool, model or handoff caused it? Two steps:

1. Evidence. Look for spans that carry a concrete signal: an error, a failed check, a denied action, an invalid
   handoff. Errors travel up and forward, so the cause is the earliest such span that has no failing span below it.
2. Statistics. Some failures leave no signal at all: every span is green but the answer was wrong. For those,
   compare journeys that failed with journeys that passed and ask which component the failures have in common.

This module never looks at chaos experiments or simulator labels. tests/test_rca.py checks that.
"""

from __future__ import annotations

from collections import Counter, defaultdict

SPECIFICITY = {"tool": 0, "model": 1, "handoff": 2, "agent": 3}  # on a tie, blame the narrower thing


def component(span: dict) -> str | None:
    kind, agent, target = span["kind"], span.get("agent"), span.get("target")
    if kind == "agent" and agent:
        return f"agent:{agent}"
    if kind in ("tool", "model") and target:
        # a call that was routed to the fallback did not touch the original, so it must not count for or against it
        fallback = " (fallback)" if (span.get("attrs") or {}).get("themis.fallback") and kind == "tool" else ""
        return f"{kind}:{target}{fallback}"
    if kind == "handoff" and agent and target:
        return f"handoff:{agent}->{target}"
    return None


def blame(span: dict) -> tuple[str, str] | None:
    """(component, why) when this span shows something wrong, else None."""
    attrs = span.get("attrs") or {}
    agent = f"agent:{span['agent']}" if span.get("agent") else None
    decision = attrs.get("themis.gate.decision")
    if decision in ("deny", "denied_by_human") and agent:
        what = "tried an action outside its policy" if decision == "deny" else "proposed an action a human rejected"
        return agent, f"{span['agent']} {what}: {span.get('target') or span.get('name')}"
    if span["kind"] == "handoff" and attrs.get("themis.handoff.valid") is False and agent:
        return agent, f"{span['agent']} handed invalid work to {span.get('target')}"
    if attrs.get("themis.check.passed") is False:
        name = attrs.get("themis.check.name", "check")
        if attrs.get("themis.check.blame") == "caller" and agent:
            return agent, f"{span['agent']} called {span.get('target')} unsafely ({name} failed)"
        if comp := component(span):
            return comp, f"{span.get('target') or span.get('name')} returned a bad result ({name} failed)"
    if not span["ok"] and span["kind"] != "other" and (comp := component(span)):
        return comp, f"{comp.split(':', 1)[1]} failed: {span.get('error_type') or 'error'}"
    return None


def attribute(spans: list[dict]) -> tuple[str, str, float] | None:
    """(culprit, reason, confidence) from the evidence in one journey's spans, or None when there is none."""
    blamed = {s["span_id"]: b for s in spans if (b := blame(s))}
    if not blamed:
        return None
    children = defaultdict(list)
    for s in spans:
        if s.get("parent_id"):
            children[s["parent_id"]].append(s["span_id"])

    def has_blamed_descendant(span_id: str) -> bool:
        stack = list(children[span_id])
        while stack:
            cur = stack.pop()
            if cur in blamed:
                return True
            stack.extend(children[cur])
        return False

    origins = [s for s in spans if s["span_id"] in blamed and not has_blamed_descendant(s["span_id"])]
    first = min(origins, key=lambda s: (s["start"], s["span_id"]))
    culprit, reason = blamed[first["span_id"]]
    return culprit, reason, 0.9 if len(origins) == 1 else 0.7


def suspicion(sample: list[tuple[bool, list[str]]], min_support: int = 8) -> dict[str, float]:
    """Score per component = risk difference x coverage.

    Risk difference is P(journey fails | it was used) - P(journey fails | it was not used). Coverage is the share
    of all failures that touched it. A tool that three agents share and the one agent that happens to call it
    have the same risk difference; the tool explains every failure and the agent only some, so the tool wins.

    sample is (failed, components) per journey. A component used by every journey, or by too few, gets no score:
    there is nothing to compare it with.
    """
    with_n, with_bad = Counter(), Counter()
    total, total_bad = len(sample), sum(1 for failed, _ in sample if failed)
    for failed, comps in sample:
        for c in set(comps or ()):
            with_n[c] += 1
            with_bad[c] += failed
    scores = {}
    for c, n in with_n.items():
        without = total - n
        if n < min_support or without < min_support:
            continue
        score = (with_bad[c] / n - (total_bad - with_bad[c]) / without) * (with_bad[c] / total_bad if total_bad else 0)
        if score > 0:
            scores[c] = round(score, 4)
    return scores


def pick(components: list[str], scores: dict[str, float], threshold: float = 0.2) -> tuple[str, str, float] | None:
    """The most suspicious component a journey touched, when the statistics are strong enough to name one."""
    ranked = sorted(
        ((scores[c], -SPECIFICITY.get(c.split(":", 1)[0], 9), c) for c in set(components or ()) if scores.get(c, 0) >= threshold),
        reverse=True,
    )
    if not ranked:
        return None
    score, _, culprit = ranked[0]
    return (
        culprit,
        f"no failing span, but {culprit} is what the unexplained failures have in common (score {score:.2f})",
        min(score, 0.8),
    )
