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
