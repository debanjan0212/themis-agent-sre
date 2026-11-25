"""The maths that decides who loses autonomy. Examples for the rules, properties for the invariants."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from themis import autonomy, slo
from themis.slo import FAST_LONG, FAST_SHORT, PERIOD


def status(sli="task_success", target=0.97, long=(100, 100), short=(10, 10), period=(1000, 1000), more=None):
    return slo.evaluate(sli, target, {FAST_LONG: long, FAST_SHORT: short, PERIOD: period, **(more or {})})


# ---------------------------------------------------------------- burn rate


def test_burn_rate_is_error_ratio_over_allowed_ratio():
    assert slo.burn_rate(97, 100, 0.97) == pytest.approx(1.0)  # spending exactly the budget
    assert round(slo.burn_rate(50, 100, 0.97), 2) == 16.67
    assert slo.burn_rate(0, 0, 0.97) == 0.0  # no traffic is not an outage


def test_page_needs_both_windows_over_the_rate():
    assert status(long=(50, 100), short=(5, 10)).firing == "page"
    # the long window is still bad but the problem has stopped: no page
    assert status(long=(50, 100), short=(10, 10)).firing is None
    # a short spike that has not moved the hour: no page
    assert status(long=(99, 100), short=(0, 10)).firing is None


def test_too_few_events_never_page():
    assert status(long=(0, 9), short=(0, 9)).firing is None  # 9 failures out of 9, but below the minimum sample
    assert status(long=(0, 10), short=(0, 2)).firing is None


def test_slow_rule_pages_when_the_fast_one_does_not():
    # 20% errors is 6.7x: under 14.4x, over 6x on the 6h and 30m windows
    s = status(long=(80, 100), short=(8, 10), more={21600: (800, 1000), 1800: (80, 100)})
    assert s.firing == "page" and "6h" in s.rule


def test_budget_left_goes_negative_when_overspent():
    assert status(period=(1000, 1000)).budget_left == 1.0
    assert round(status(period=(970, 1000)).budget_left, 6) == 0.0
    assert status(period=(900, 1000)).budget_left < 0


counts = st.integers(0, 5000).flatmap(lambda total: st.tuples(st.integers(0, total), st.just(total)))


@given(long=counts, short=counts, target=st.floats(0.5, 0.9999))
def test_properties_of_evaluate(long, short, target):
    s = slo.evaluate("task_success", target, {FAST_LONG: long, FAST_SHORT: short, PERIOD: long})
    assert all(b >= 0 for b in s.burns.values())
    assert s.budget_left <= 1.0
    if long[1] < slo.MIN_LONG or short[1] < slo.MIN_SHORT:
        assert s.firing is None
    if long[0] == long[1] or short[0] == short[1]:  # a window with zero errors can never page
        assert s.firing != "page" or s.burns[FAST_LONG] >= 6


@given(bad=st.integers(0, 99))
def test_more_errors_never_lower_the_burn(bad):
    assert slo.burn_rate(100 - bad - 1, 100, 0.97) >= slo.burn_rate(100 - bad, 100, 0.97)


# ---------------------------------------------------------------- the autonomy ladder

CALM = [status()]
BURNING = [status(long=(50, 100), short=(5, 10))]
STILL_BURNING = [status(long=(50, 100), short=(10, 30))]  # 30 events in the short window, two thirds failing


def test_demotion_and_recovery_path():
    assert autonomy.decide("autonomous", 0, CALM, 0) is None
    assert autonomy.decide("autonomous", 0, BURNING, 0).level == "supervised"
    # supervised: stays put through the grace period, then is suspended if the short window is still burning
    assert autonomy.decide("supervised", 599, STILL_BURNING, 0) is None
    assert autonomy.decide("supervised", 600, STILL_BURNING, 0).level == "suspended"
    # supervised and calm: trusted again only after the cool-down
    assert autonomy.decide("supervised", 1799, CALM, 0) is None
    assert autonomy.decide("supervised", 1800, CALM, 0).level == "autonomous"


def test_a_handful_of_events_cannot_suspend():
    few = [status(long=(50, 100), short=(0, 9))]  # 9 of 9 failed in the short window: alarming, but not a sample
    assert autonomy.decide("supervised", 10_000, few, 0) is None


def test_policy_violations_suspend_at_once_from_any_trusted_level():
    for level in ("autonomous", "supervised"):
        move = autonomy.decide(level, 0, CALM, 5)
        assert move.level == "suspended" and "policy" in move.reason
    assert autonomy.decide("autonomous", 0, CALM, 4) is None


def test_no_traffic_means_no_promotion():
    silent = [slo.evaluate("task_success", 0.97, {})]
    assert autonomy.decide("supervised", 10_000, silent, 0) is None


levels = st.sampled_from(autonomy.LEVELS)
statuses = st.lists(st.builds(lambda long, short: status(long=long, short=short), counts, counts), min_size=1, max_size=4)


@given(level=levels, seconds=st.floats(0, 1e6), sts=statuses, violations=st.integers(0, 20))
def test_ladder_invariants(level, seconds, sts, violations):
    move = autonomy.decide(level, seconds, sts, violations)
    if level == "suspended":
        assert move is None  # only a human gives autonomy back
    if move is None:
        return
    assert move.level != level and move.reason
    if move.level == "autonomous":  # promotion needs every indicator calm and no violations
        assert violations < 5 and all(s.burns[FAST_LONG] < 1 and s.burns[FAST_SHORT] < 1 for s in sts)
    # no flapping: the same evidence, one tick later, does not move the agent straight back
    again = autonomy.decide(move.level, 0, sts, violations)
    assert again is None or again.level != level
