"""SLO maths. Pure functions, no I/O.

An agent has four service level indicators, each "good events / all events":
  task_success     the agent finished its task without an error
  handoff_success  what it handed to the next agent was valid
  safe_actions     its high-risk actions passed their check
  cost             its task stayed under the agent's cost ceiling
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SLOS = {"task_success": 0.97, "handoff_success": 0.98, "safe_actions": 0.999, "cost": 0.95}

# Multi-window, multi-burn-rate alerts (Google SRE workbook, ch. 5): (burn rate, long window s, short window s, action).
# Same table IntentOps writes for services, so an agent and a service are judged by one rule.
BURN_RULES = ((14.4, 3600, 300, "page"), (6.0, 21600, 1800, "page"), (1.0, 259200, 21600, "ticket"))
PERIOD = 30 * 86400
FAST_LONG, FAST_SHORT = 3600, 300
MIN_LONG, MIN_SHORT = 10, 3  # fewer events than this in a window is noise, not a signal


def burn_rate(good: int, total: int, target: float) -> float:
    """How many times faster than allowed the error budget is being spent. 1.0 = exactly on budget."""
    if total <= 0:
        return 0.0
    return ((total - good) / total) / (1.0 - target)


@dataclass(frozen=True)
class SliStatus:
    sli: str
    target: float
    value: float | None  # the indicator over the fast long window; None when there is no data yet
    burns: dict[int, float] = field(default_factory=dict)  # window seconds -> burn rate
    samples: dict[int, int] = field(default_factory=dict)  # window seconds -> events seen
    budget_left: float = 1.0  # share of the period's error budget still unspent; negative when overspent
    firing: str | None = None  # "page", "ticket" or None
    rule: str = ""

    def as_dict(self) -> dict:
        return {
            "sli": self.sli,
            "target": self.target,
            "value": self.value,
            "burn_long": round(self.burns.get(FAST_LONG, 0.0), 2),
            "burn_short": round(self.burns.get(FAST_SHORT, 0.0), 2),
            "samples": self.samples.get(FAST_LONG, 0),
            "budget_left": round(self.budget_left, 4),
            "firing": self.firing,
            "rule": self.rule,
        }


def evaluate(sli: str, target: float, counts: dict[int, tuple[int, int]]) -> SliStatus:
    """counts maps a window in seconds to (good, total) for that window."""
    burns = {w: burn_rate(g, t, target) for w, (g, t) in counts.items()}
    samples = {w: t for w, (_, t) in counts.items()}
    firing, rule = None, ""
    for rate, long, short, action in BURN_RULES:
        enough = samples.get(long, 0) >= MIN_LONG and samples.get(short, 0) >= MIN_SHORT
        if enough and burns.get(long, 0.0) >= rate and burns.get(short, 0.0) >= rate:
            firing = action
            rule = (
                f"{sli.replace('_', ' ')} is burning its budget {burns[long]:.1f}x too fast ({_human(long)}) "
                f"and {burns[short]:.1f}x ({_human(short)})"
            )
            break
    good, total = counts.get(FAST_LONG, (0, 0))
    period_good, period_total = counts.get(PERIOD, (0, 0))
    return SliStatus(
        sli=sli,
        target=target,
        value=(good / total) if total else None,
        burns=burns,
        samples=samples,
        budget_left=1.0 - burn_rate(period_good, period_total, target),
        firing=firing,
        rule=rule,
    )


def _human(seconds: int) -> str:
    return f"{seconds // 86400}d" if seconds >= 86400 else f"{seconds // 3600}h" if seconds >= 3600 else f"{seconds // 60}m"
