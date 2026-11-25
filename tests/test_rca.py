"""Root cause: evidence first, statistics for what leaves no evidence."""

import ast
from pathlib import Path

from themis import rca


def span(sid, kind, parent="root", agent=None, target=None, ok=True, start=0.0, error=None, **attrs):
    return dict(
        span_id=sid,
        parent_id=parent,
        kind=kind,
        name=sid,
        agent=agent,
        target=target,
        ok=ok,
        start=start,
        error_type=error,
        attrs=attrs,
    )


ROOT = span("root", "other", parent=None, ok=False)


def test_an_error_is_blamed_on_the_deepest_span_that_failed():
    # the model failed, so the agent failed, so the journey failed: the model is the cause, not the agent
    spans = [
        ROOT,
        span("a", "agent", agent="planner", ok=False, start=1),
        span("m", "model", parent="a", agent="planner", target="sim-large", ok=False, start=2, error="rate_limited"),
    ]
    culprit, reason, confidence = rca.attribute(spans)
    assert culprit == "model:sim-large" and "rate_limited" in reason and confidence == 0.9


def test_an_invalid_handoff_blames_the_sender_not_the_agent_that_choked_on_it():
    spans = [
        ROOT,
        span("a", "agent", agent="coder", start=1),
        span("h", "handoff", parent="a", agent="coder", target="reviewer", start=2, **{"themis.handoff.valid": False}),
        span("b", "agent", agent="reviewer", ok=False, start=3, error="bad_input"),
    ]
    assert rca.attribute(spans)[0] == "agent:coder"


def test_a_failed_check_blames_the_tool_unless_the_caller_misused_it():
    bad_result = span("t", "tool", parent="a", agent="researcher", target="kb.search", start=2, **{"themis.check.passed": False})
    assert rca.attribute([ROOT, span("a", "agent", agent="researcher", start=1), bad_result])[0] == "tool:kb.search"
    misuse = dict(
        bad_result, target="deploy.apply", agent="deployer", attrs={"themis.check.passed": False, "themis.check.blame": "caller"}
    )
    assert rca.attribute([ROOT, span("a", "agent", agent="deployer", start=1), misuse])[0] == "agent:deployer"


def test_a_denied_action_blames_the_agent_that_tried_it():
    for decision in ("deny", "denied_by_human"):
        denied = span(
            "t", "tool", parent="a", agent="coder", target="secrets.read", ok=False, start=2, **{"themis.gate.decision": decision}
        )
        assert rca.attribute([ROOT, span("a", "agent", agent="coder", ok=False, start=1), denied])[0] == "agent:coder"


def test_the_runtime_reason_for_a_block_is_quoted():
    why = "Themis has suspended deployer, so a person must take this action"
    blocked = span("t", "tool", parent="a", agent="deployer", target="rollback", ok=False, start=2)
    blocked["attrs"] = {"themis.gate.decision": "deny", "themis.gate.reason": why}
    culprit, reason, _ = rca.attribute([ROOT, span("a", "agent", agent="deployer", start=1), blocked])
    assert culprit == "agent:deployer" and reason == f"deployer was stopped before rollback: {why}"


def test_a_call_that_went_to_the_fallback_is_a_different_component():
    call = span("t", "tool", agent="researcher", target="kb.search", **{"themis.fallback": True})
    assert rca.component(call) == "tool:kb.search (fallback)"
    assert rca.component(dict(call, attrs={})) == "tool:kb.search"


def test_no_evidence_means_no_answer():
    assert rca.attribute([ROOT, span("a", "agent", agent="scribe", start=1)]) is None


def test_simulator_labels_cannot_change_the_answer():
    """The ground truth rides on the root span. The answer must be the same whatever that label says."""
    spans = [ROOT, span("a", "agent", agent="planner", ok=False, start=1, error="task_error")]
    lied = [dict(ROOT, attrs={"sim.truth": "tool:kb.search"}), *spans[1:]]
    assert rca.attribute(spans) == rca.attribute(lied) == ("agent:planner", "planner failed: task_error", 0.9)


def test_rca_module_cannot_see_chaos_or_the_database():
    imported = {
        n.module if isinstance(n, ast.ImportFrom) else n.names[0].name
        for n in ast.walk(ast.parse(Path(rca.__file__).read_text()))
        if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    assert imported == {"__future__", "collections"}


# ---------------------------------------------------------------- statistics


def journeys(n, comps, failed=0):
    return [(i < failed, comps) for i in range(n)]


def test_shared_tool_beats_the_agents_that_call_it():
    # kb.search is silently wrong. planner and researcher both call it; scribe is in every journey.
    sample = (
        journeys(40, ["agent:planner", "tool:kb.search", "agent:scribe"], failed=16)
        + journeys(40, ["agent:researcher", "tool:kb.search", "agent:scribe"], failed=16)
        + journeys(40, ["agent:researcher", "tool:web.fetch", "agent:scribe"])
        + journeys(40, ["agent:finops", "agent:scribe"])
    )
    scores = rca.suspicion(sample)
    assert max(scores, key=scores.get) == "tool:kb.search"
    assert "agent:scribe" not in scores  # in every journey: nothing to compare it with
    assert "tool:web.fetch" not in scores and "agent:finops" not in scores  # never in a failure
    culprit, reason, confidence = rca.pick(["agent:planner", "tool:kb.search", "agent:scribe"], scores)
    assert culprit == "tool:kb.search" and confidence <= 0.8


def test_statistics_stay_silent_without_enough_journeys_or_signal():
    assert rca.suspicion(journeys(5, ["tool:x"], failed=5) + journeys(5, ["tool:y"])) == {}
    weak = rca.suspicion(journeys(100, ["tool:x"], failed=3) + journeys(100, ["tool:y"], failed=2))
    assert rca.pick(["tool:x"], weak) is None
