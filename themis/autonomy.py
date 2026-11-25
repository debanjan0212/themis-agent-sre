"""The autonomy ladder. Pure functions, no I/O.

  autonomous  the agent acts on its own
  supervised  high-risk actions wait for a human go / no-go
  suspended   the agent is not called at all; only a human puts it back

An agent moves down when it spends its error budget too fast or breaks policy, and back up when it has
behaved for a while. Suspended never clears itself: a machine may take autonomy away, only a person gives it back.
"""

from __future__ import annotations

from dataclasses import dataclass

from themis.slo import BURN_RULES, FAST_LONG, FAST_SHORT, MIN_LONG, SliStatus

LEVELS = ("autonomous", "supervised", "suspended")


@dataclass(frozen=True)
class Policy:
    violation_limit: int = 5  # policy denials inside the short window that mean "this agent has gone rogue"
    supervised_grace: float = 600  # seconds a supervised agent gets before "still burning" suspends it
    cooldown: float = 1800  # seconds of good behaviour before a supervised agent is trusted again


@dataclass(frozen=True)
class Move:
    level: str
    reason: str


def decide(
    level: str, seconds_in_level: float, statuses: list[SliStatus], violations: int, policy: Policy = Policy()
) -> Move | None:
    """The next level for an agent, or None to leave it where it is. Times are production seconds."""
    if level == "suspended":
        return None
    if violations >= policy.violation_limit:
        return Move("suspended", f"{violations} policy violations in 5m: the agent is acting outside its allow-list")
    if level == "autonomous":
        paging = next((s for s in statuses if s.firing == "page"), None)
        return Move("supervised", paging.rule) if paging else None
    # supervised from here on
    fast = BURN_RULES[0][0]
    # the short window alone decides here (the long one still holds the failures that caused the demotion),
    # so it must hold a real sample: a handful of events can read as a 20x burn by chance
    still = next(
        (s for s in statuses if s.samples.get(FAST_SHORT, 0) >= MIN_LONG and s.burns.get(FAST_SHORT, 0.0) >= fast),
        None,
    )
    if still and seconds_in_level >= policy.supervised_grace:
        name = still.sli.replace("_", " ")
        return Move("suspended", f"{name} still burning {still.burns[FAST_SHORT]:.1f}x (5m) while supervised")
    calm = all(s.burns.get(FAST_LONG, 0.0) < 1.0 and s.burns.get(FAST_SHORT, 0.0) < 1.0 for s in statuses)
    seen = any(s.samples.get(FAST_LONG, 0) >= MIN_LONG for s in statuses)
    if calm and seen and seconds_in_level >= policy.cooldown:
        return Move("autonomous", "every SLO back under 1x burn for the cool-down period")
    return None
