"""Themis with its sibling projects, running for real: agent-harness-sdk, Aegis War Room, Argus and IntentOps.

Skipped unless you point at the clones and a Python that has them installed:

    THEMIS_SIBLINGS=/path/to/folder-with-the-four-repos THEMIS_SIBLINGS_PYTHON=/path/to/venv/bin/python pytest -m ecosystem

`make ecosystem` clones and installs them for you. CI does the same with the commits pinned in the workflow.
"""

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from tests.conftest import free_port, wait_for
from themis.lab import Lab

SIBLINGS = Path(os.getenv("THEMIS_SIBLINGS", "/nonexistent"))
PY = os.getenv("THEMIS_SIBLINGS_PYTHON", "")
REPO = Path(__file__).parent.parent
pytestmark = [
    pytest.mark.ecosystem,
    pytest.mark.skipif(
        not PY or not (SIBLINGS / "agent-harness-sdk").is_dir(), reason="set THEMIS_SIBLINGS and THEMIS_SIBLINGS_PYTHON"
    ),
]


def test_agent_harness_sdk_agents_are_observed_with_no_code_change(tmp_path):
    """The SDK already emits OpenTelemetry GenAI spans. Two environment variables point them at Themis."""
    lab = Lab(f"sqlite:///{tmp_path}/t.db", free_port(), 120.0, eval_interval=0.3, settle_seconds=0.2).start()
    try:
        key = lab.new_key("default", ["ingest"])
        env = {
            **os.environ,
            "OTEL_EXPORTER_OTLP_ENDPOINT": lab.url,
            "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer%20{key}",
        }
        sdk = SIBLINGS / "agent-harness-sdk"
        subprocess.run([PY, "examples/quickstart.py"], cwd=sdk, env=env, check=True, capture_output=True, timeout=120)
        demo = [PY, "-m", "incident_commander", "demo", "--scenario", "bad-deploy", "--approve", "auto"]
        subprocess.run(demo, cwd=sdk / "examples", env=env, check=True, capture_output=True, timeout=120)

        auth = {"Authorization": f"Bearer {lab.key}"}
        journeys = wait_for(
            lambda: (js := httpx.get(f"{lab.url}/api/journeys", headers=auth).json()["journeys"]) and len(js) == 2 and js,
            15,
            "both SDK runs to be judged",
        )
        used = {}
        for j in journeys:
            assert j["outcome"] == "success" and j["tokens"] > 0
            for u in httpx.get(f"{lab.url}/api/journeys/{j['trace_id']}", headers=auth).json()["uses"]:
                used[u["agent"]] = u
        # the SDK does not put the agent's name on model and tool spans; Themis works it out from the trace
        assert used["disk-janitor"]["tools"] == ["disk_usage", "rotate_logs"] and used["disk-janitor"]["models"] == ["scripted"]
        assert "rollback_deploy" in used["incident-commander"]["tools"] and used["incident-commander"]["models"]
        agents = {a["name"]: a for a in httpx.get(f"{lab.url}/api/overview", headers=auth).json()["agents"]}
        assert agents["incident-commander"]["autonomy"] == "autonomous"
        assert agents["incident-commander"]["slis"][0] == {
            **agents["incident-commander"]["slis"][0],
            "sli": "task_success",
            "value": 1.0,
        }
    finally:
        lab.stop()


def test_an_incident_reaches_aegis_argus_and_incident_commander(tmp_path):
    """One Themis incident, three receivers, each started from its own repository."""
    ports = {name: free_port() for name in ("aegis", "argus", "commander")}
    token = os.urandom(16).hex()
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    servers = [
        subprocess.Popen(
            [PY, "-m", "aegis", "serve", "--host", "127.0.0.1", "--port", str(ports["aegis"])],
            cwd=SIBLINGS / "aegis-war-room",
            env={**os.environ, "AEGIS_API_TOKEN": token, "AEGIS_DB": str(tmp_path / "aegis.db")},
            **quiet,
        ),
        subprocess.Popen(
            [PY, "-m", "argus", "serve"],
            cwd=SIBLINGS / "argus-sre-agent",
            env={**os.environ, "ARGUS_PORT": str(ports["argus"]), "ARGUS_HOST": "127.0.0.1"},
            **quiet,
        ),
        subprocess.Popen(
            [PY, "-m", "incident_commander", "serve", "--port", str(ports["commander"])],
            cwd=SIBLINGS / "agent-harness-sdk" / "examples",
            env={**os.environ, "HOST": "127.0.0.1"},
            **quiet,
        ),
    ]
    aegis, argus, commander = (f"http://127.0.0.1:{ports[n]}" for n in ("aegis", "argus", "commander"))
    hooks = (f"{aegis}/api/alerts", f"{argus}/alertmanager", f"{commander}/webhooks/alertmanager")
    lab = None
    try:

        def up(url):
            try:
                return httpx.get(url, timeout=1).status_code < 500
            except httpx.HTTPError:
                return False

        for url in (f"{aegis}/api/config", f"{argus}/healthz", f"{commander}/healthz"):
            wait_for(lambda u=url: up(u), 40, f"{url} to answer")

        settings = dict(webhooks=hooks, webhook_allow_private=True, webhook_token=token, eval_interval=0.3, settle_seconds=0.2)
        lab = Lab(f"sqlite:///{tmp_path}/t.db", free_port(), 120.0, **settings).start()
        auth = {"Authorization": f"Bearer {lab.key}"}
        httpx.put(f"{lab.url}/api/fleet", json={"agents": {"payment": {"model": "m1"}}}, headers=auth).raise_for_status()
        body = {"level": "suspended", "reason": "error budget gone after a bad release"}
        httpx.post(f"{lab.url}/api/agents/payment/autonomy", json=body, headers=auth).raise_for_status()

        def sent():
            (incident,) = httpx.get(f"{lab.url}/api/incidents", headers=auth).json()["incidents"]
            return incident["notify_state"] == "sent" and incident

        incident = wait_for(sent, 20, "all three receivers to accept the webhook")

        # Aegis War Room opened a war-room incident for the suspended agent
        opened = wait_for(lambda: httpx.get(f"{aegis}/api/incidents").json(), 15, "Aegis to open an incident")
        assert "payment" in json.dumps(opened)
        # Argus accepted the alert and filed an investigation under Themis's incident id
        reports = wait_for(lambda: httpx.get(f"{argus}/reports").json(), 30, "Argus to file a report")
        assert incident["id"] in json.dumps(reports)
        # Incident Commander (built on agent-harness-sdk) opened an incident too
        handled = wait_for(lambda: httpx.get(f"{commander}/incidents").json(), 15, "Incident Commander to open an incident")
        assert "payment" in json.dumps(handled)
    finally:
        if lab:
            lab.stop()
        for server in servers:
            server.terminate()
        for server in servers:
            server.wait(10)


GOVERNED = """
import json, sys
from agent_harness_sdk import (Agent, ModelReply, ScriptedModel, ThemisApprover, ThemisGate, ThemisPolicy, ToolCall,
                               setup_telemetry, tool)

setup_telemetry("governed", console=None)


@tool()
def get_errors(service: str) -> dict:
    \"\"\"Error rate for a service.\"\"\"
    return {"service": service, "error_rate": 0.31}


@tool(mutating=True)
def rollback(service: str, version: str) -> dict:
    \"\"\"Roll a service back.\"\"\"
    return {"rolled_back": service}


@tool(mutating=True, requires_approval=False)
def restart(service: str) -> dict:
    \"\"\"Restart a service.\"\"\"
    return {"restarted": service}


def brain(action, arguments):
    def fn(messages, tools, json_mode):
        stage = messages[0]["content"]
        if "Stage READ" in stage:
            if any(m["role"] == "tool" for m in messages):
                return "errors are up"
            return ModelReply(None, [ToolCall("1", "get_errors", {"service": "checkout"})])
        if "Stage ANALYSE" in stage:
            return {"summary": "checkout is failing", "likely_cause": "bad deploy", "confidence": 0.9}
        return {"action": action, "arguments": arguments, "rationale": "fix it", "confidence": 0.9, "risk": "medium"}

    return ScriptedModel(fn)


url, key, name, action, times = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
arguments = {"rollback": {"service": "checkout", "version": "1.2"}, "restart": {"service": "checkout"}}[action]
out = []
with ThemisGate(url, key) as gate:
    agent = Agent(name, model=brain(action, arguments), tools=[get_errors, rollback, restart],
                  policy=ThemisPolicy(gate=gate), approver=ThemisApprover(gate, timeout_s=60, poll_s=0.2))
    for _ in range(times):
        run = agent.run("checkout is failing")
        out.append({"outcome": run.outcome.status, "reasons": run.outcome.reasons,
                    "approved_by": run.outcome.approval.by if run.outcome.approval else None})
print(json.dumps(out))
"""


def test_agent_harness_sdk_agents_obey_themis(tmp_path):
    """The SDK's ThemisPolicy and ThemisApprover against the real control plane: an agent that keeps reaching outside
    its allow-list is suspended by Themis, the SDK then stops its actions, and a supervised agent's action waits for a
    person in Themis before it runs."""
    script = tmp_path / "governed.py"
    script.write_text(GOVERNED)
    # real-time windows: the five violations must land inside Themis's five-minute window
    lab = Lab(f"sqlite:///{tmp_path}/t.db", free_port(), 1.0, eval_interval=0.3, settle_seconds=0.2).start()
    try:
        auth = {"Authorization": f"Bearer {lab.key}"}
        key = lab.new_key("default", ["ingest"])
        env = {
            **os.environ,
            "OTEL_EXPORTER_OTLP_ENDPOINT": lab.url,
            "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer%20{key}",
        }
        fleet = {name: {"tools": ["get_errors", "restart"], "high_risk": ["rollback"]} for name in ("deployer", "releaser")}
        httpx.put(f"{lab.url}/api/fleet", json={"agents": fleet}, headers=auth).raise_for_status()

        def run(name, action, times=1):
            done = subprocess.run(
                [PY, str(script), lab.url, key, name, action, str(times)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return json.loads(done.stdout.strip().splitlines()[-1])

        def gate_says(name):
            agents = httpx.get(f"{lab.url}/v1/gate/snapshot", headers={"Authorization": f"Bearer {key}"}).json()["agents"]
            return agents[name]["autonomy"]

        def autonomy(name):
            agents = httpx.get(f"{lab.url}/api/overview", headers=auth).json()["agents"]
            return next(a["autonomy"] for a in agents if a["name"] == name)

        # five reaches outside the allow-list: each is stopped by the SDK and reported to Themis
        attempts = run("deployer", "rollback", 5)
        assert [a["outcome"] for a in attempts] == ["escalated"] * 5
        assert "not in deployer's allow-list in Themis" in attempts[0]["reasons"][0]
        wait_for(lambda: autonomy("deployer") == "suspended", 20, "Themis to suspend the deployer")
        # a runtime acts on the snapshot it last pulled (cached up to half a second), so wait for the snapshot too
        wait_for(lambda: gate_says("deployer") == "suspended", 10, "the gate snapshot to say suspended")

        # suspended: even a tool on its allow-list now goes to a person
        (after,) = run("deployer", "restart")
        assert after["outcome"] == "escalated" and "suspended deployer" in after["reasons"][0]

        # supervised: the action waits for a person in Themis, and runs once they say go
        body = {"level": "supervised", "reason": "new release, watch it"}
        httpx.post(f"{lab.url}/api/agents/releaser/autonomy", json=body, headers=auth).raise_for_status()
        wait_for(lambda: gate_says("releaser") == "supervised", 10, "the gate snapshot to say supervised")
        cmd = [PY, str(script), lab.url, key, "releaser", "restart", "1"]
        waiting = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            pending = wait_for(
                lambda: [
                    a for a in httpx.get(f"{lab.url}/api/approvals", headers=auth).json()["approvals"] if a["status"] == "pending"
                ],
                30,
                "the releaser's action to reach Themis",
            )
            assert pending[0]["agent"] == "releaser" and pending[0]["action"] == "tool:restart"
            decision = {"approve": True, "by": "debanjan"}
            httpx.post(f"{lab.url}/api/approvals/{pending[0]['id']}/decision", json=decision, headers=auth).raise_for_status()
            out, _ = waiting.communicate(timeout=60)
        finally:
            waiting.kill()
        (approved,) = json.loads(out.strip().splitlines()[-1])
        assert approved == {"outcome": "done", "reasons": [], "approved_by": "themis"}

        # and Themis read the stopped runs as blocked, with the deployer named as the cause and the SDK's reason
        def blocked():
            js = httpx.get(f"{lab.url}/api/journeys", headers=auth).json()["journeys"]
            return [j for j in js if j["outcome"] == "blocked"]

        stopped = wait_for(lambda: len(b := blocked()) == 6 and b, 20, "the six stopped runs to be judged")
        assert {j["culprit"] for j in stopped} == {"agent:deployer"}
        assert any("suspended deployer" in (j["culprit_reason"] or "") for j in stopped)
    finally:
        lab.stop()


def test_aegis_and_argus_report_their_own_agents(tmp_path):
    """The responders are agents too: with the OpenTelemetry variables set, Themis sees every one of them."""
    lab = Lab(f"sqlite:///{tmp_path}/t.db", free_port(), 120.0, eval_interval=0.3, settle_seconds=0.2).start()
    try:
        key = lab.new_key("default", ["ingest"])
        env = {
            **os.environ,
            "OTEL_EXPORTER_OTLP_ENDPOINT": lab.url,
            "OTEL_EXPORTER_OTLP_HEADERS": f"Authorization=Bearer%20{key}",
            "AEGIS_PACE": "0",
            "AEGIS_AUDIT_LOG": str(tmp_path / "aegis-audit.jsonl"),
        }
        aegis = [PY, "-m", "aegis", "demo", "--scenario", "payment-failure", "--approve", "go"]
        subprocess.run(aegis, cwd=SIBLINGS / "aegis-war-room", env=env, check=True, capture_output=True, timeout=120)
        argus = [PY, "-m", "argus", "investigate", "--scenario", "evals/scenarios/02-oom-kill.yaml"]
        subprocess.run(argus, cwd=SIBLINGS / "argus-sre-agent", env=env, check=True, capture_output=True, timeout=120)

        auth = {"Authorization": f"Bearer {lab.key}"}
        journeys = wait_for(
            lambda: (js := httpx.get(f"{lab.url}/api/journeys", headers=auth).json()["journeys"]) and len(js) == 3 and js,
            20,
            "the two Aegis traces and the Argus trace to be judged",
        )
        assert {j["outcome"] for j in journeys} == {"success"}
        by_name = {j["name"]: j for j in journeys}
        assert by_name["incident investigate"]["session_id"] == by_name["incident act"]["session_id"]
        uses = {
            u["agent"]: u
            for u in httpx.get(f"{lab.url}/api/journeys/{by_name['incident act']['trace_id']}", headers=auth).json()["uses"]
        }
        assert uses["executor"]["tools"] == ["set_feature_flag"]
        detail = httpx.get(f"{lab.url}/api/journeys/{by_name['incident act']['trace_id']}", headers=auth).json()
        decisions = {sp["attrs"].get("themis.gate.decision") for sp in detail["spans"]} - {None}
        assert decisions == {"approved_by_human"}
        names = {a["name"] for a in httpx.get(f"{lab.url}/api/overview", headers=auth).json()["agents"]}
        crew = {"triage", "signals", "changes", "runbook", "planner", "defender", "executor", "verifier", "scribe"}
        assert crew | {"argus"} <= names
    finally:
        lab.stop()


def test_intentops_plans_checks_and_renders_the_themis_deployment(tmp_path):
    """deploy/intent.yaml is the source of truth for the Kubernetes manifests in deploy/kubernetes."""
    intentops = [PY, "-m", "intentops"]
    intent = str(REPO / "deploy" / "intent.yaml")
    plan = subprocess.run([*intentops, "plan", intent], capture_output=True, text=True, check=True).stdout
    assert "runs 3-8 replicas" in plan and "0 fail" in plan
    subprocess.run([*intentops, "check", intent], check=True, capture_output=True)  # exit 1 on any policy failure
    subprocess.run([*intentops, "render", intent, "--format", "manifests", "-o", str(tmp_path)], check=True, capture_output=True)
    docs = [d for f in sorted(tmp_path.rglob("*.yaml")) for d in yaml.safe_load_all(f.read_text()) if d]
    kinds = {d["kind"] for d in docs}
    # a canary Rollout rather than a Deployment, with the SLO burn alerts and the secret references next to it
    assert {
        "Rollout",
        "Service",
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "NetworkPolicy",
        "ExternalSecret",
        "PrometheusRule",
    } <= kinds
    committed = [
        d for f in sorted((REPO / "deploy" / "kubernetes").glob("*.yaml")) for d in yaml.safe_load_all(f.read_text()) if d
    ]
    assert committed == docs, "deploy/kubernetes is stale: run `make render`"
