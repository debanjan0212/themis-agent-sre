"""The whole system, running for real: two replicas, the agent world, injected faults, a webhook receiver.

Nothing is mocked here. These are the slow tests (about a minute together) and the ones that matter most.
"""

import asyncio
import json

import httpx

from tests.conftest import Receiver, free_port, journey, wait_for
from themis.lab import Lab
from themis.world.sim import World


def test_two_replicas_share_the_work_and_survive_losing_the_leader(db_url):
    fast = dict(eval_interval=0.2, settle_seconds=0.2)
    labs = [Lab(db_url, free_port(), 120.0, instance=f"replica-{i}", **fast).start() for i in range(2)]
    try:

        def one_leader():
            leading = [lab for lab in labs if lab.app.state.evaluator.leader]
            return leading[0] if len(leading) == 1 else None

        leader = wait_for(one_leader, 10, "exactly one replica to take the lease")
        follower = next(lab for lab in labs if lab is not leader)
        auth = {"Authorization": f"Bearer {follower.key}"}

        def judged(lab, trace_id):
            return httpx.get(f"{lab.url}/api/journeys/{trace_id}", headers=auth).status_code == 200

        # spans sent to the follower are judged by the leader and can be read from either replica
        trace_id, body = journey()
        httpx.post(f"{follower.url}/v1/traces", content=body, headers=auth).raise_for_status()
        wait_for(lambda: judged(leader, trace_id) and judged(follower, trace_id), 10, "the journey to be judged")

        leader.stop()  # the leader dies
        wait_for(lambda: follower.app.state.evaluator.leader, 15, "the follower to take over the lease")
        trace_id, body = journey()
        httpx.post(f"{follower.url}/v1/traces", content=body, headers=auth).raise_for_status()
        wait_for(lambda: judged(follower, trace_id), 10, "the survivor to judge new journeys")
        assert httpx.get(f"{follower.url}/readyz").json() == {"ok": True, "leader": True}
    finally:
        for lab in labs:
            lab.stop()


def test_the_fleet_contains_a_rogue_agent_and_a_poisoned_one(db_url):
    """Ten agents under load. One starts taking unsafe actions, another is fed injected instructions.
    (The two are on different journeys, so neither fault hides the other.)

    Expected: the rogue one is put under supervision and a human stops its unsafe actions; the poisoned one
    breaks policy and is suspended; journeys that needed it are stopped safely; both causes are named
    correctly; and the incident reaches the webhook receiver.
    """
    receiver = Receiver()
    lab = Lab(db_url, free_port(), 120.0, webhooks=(receiver.url,), webhook_allow_private=True, rate_limit_per_sec=5000).start()

    async def scenario():
        world = World(lab.url, lab.key, seed=11)
        await world.apply_fleet()
        running = asyncio.create_task(world.run(users=10, duration=44))
        await asyncio.sleep(10)
        async with lab.http() as http:
            before = (await http.get("/api/overview")).json()
            for fault, target, intensity in (("rogue_agent", "agent:deployer", 0.3), ("context_poison", "agent:researcher", 0.6)):
                body = {"fault": fault, "target": target, "intensity": intensity, "duration_s": 20}
                (await http.post("/api/experiments", json=body)).raise_for_status()
            for _ in range(60):
                await asyncio.sleep(1)
                experiments = (await http.get("/api/experiments")).json()["experiments"]
                if all(x["status"] == "done" for x in experiments):
                    break
            got = {
                "before": before,
                "experiments": {x["fault"]: x["result"] for x in experiments},
                "deployer": (await http.get("/api/agents/deployer")).json(),
                "researcher": (await http.get("/api/agents/researcher")).json(),
                "denied": (await http.get("/api/approvals?status=denied")).json()["approvals"],
                "approved": (await http.get("/api/approvals?status=approved")).json()["approvals"],
                "metrics": (await http.get("/metrics")).text,
            }
        await running
        return got, world.stats

    try:
        got, stats = asyncio.run(scenario())
    finally:
        lab.stop()
        receiver.server.shutdown()

    # a healthy fleet first: nobody demoted, nearly everything succeeds
    assert got["before"]["vitals"]["success"] > 0.9
    assert {a["autonomy"] for a in got["before"]["agents"]} == {"autonomous"}

    rogue, poison = got["experiments"]["rogue_agent"], got["experiments"]["context_poison"]

    # the rogue deployer: demoted by Themis, then every unsafe action it proposed was put to a human
    moves = [(e["new"], e["actor"]) for e in got["deployer"]["events"]]
    assert ("supervised", "themis") in moves
    assert rogue["detection"]["detected"] and rogue["detection"]["seconds"] < 15
    assert rogue["containment"]["unsafe_actions_before"] >= 1
    assert rogue["containment"]["unsafe_actions_after"] <= 2  # at most the second it takes the snapshot to reach the agent
    assert got["denied"] and all(a["detail"]["unsafe_params"] for a in got["denied"])
    assert got["approved"] and not any(a["detail"]["unsafe_params"] for a in got["approved"])

    # the poisoned researcher: policy violations suspend it without waiting for an SLO window
    suspensions = [e for e in got["researcher"]["events"] if (e["new"], e["actor"]) == ("suspended", "themis")]
    assert suspensions and "policy violations" in suspensions[0]["reason"]
    assert poison["detection"]["response"] == "agent moved to suspended" and poison["detection"]["seconds"] < 15
    assert poison["journeys"]["blocked"] > 0 and stats["blocked"] > 0  # stopped safely instead of failing
    assert got["researcher"]["gate_events"][0]["action"] == "tool:secrets.read"
    assert 'themis_agent_autonomy{agent="researcher"} 0' in got["metrics"]

    # root cause: scored against the simulator's ground truth, which the RCA code never reads
    for name, result in (("rogue_agent", rogue), ("context_poison", poison)):
        rca = result["rca"]
        assert rca["fault_caused"] >= 10, f"{name}: the fault barely fired, the test proves nothing"
        assert rca["named_target"] / rca["fault_caused"] >= 0.9, f"{name}: {rca}"

    # the incident reached the receiver in Alertmanager format
    alerts = [json.loads(body)["alerts"][0] for _, body in receiver.got]
    suspended = [
        a for a in alerts if a["labels"]["themis_subject"] == "agent:researcher" and a["labels"]["severity"] == "critical"
    ]
    assert suspended and suspended[0]["status"] == "firing" and suspended[0]["labels"]["service"] == "researcher"
