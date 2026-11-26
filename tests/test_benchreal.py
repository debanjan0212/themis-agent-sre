"""`themis bench-real`: scoring and the write-up, and one whole run against a stand-in cluster.

The real thing needs a Kubernetes cluster and runs in .github/workflows/bench-real.yml. Here the cluster is a fake
kubectl that keeps deployment images in a file, and Prometheus is a small server that pages while checkout runs the
bad release. Everything else is real: Themis, the on-call agent on agent-harness-sdk, Argus's MCP server (on its
recorded bad-deploy incident), and git.
"""

import importlib.util
import json
import os
import shutil
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.conftest import free_port
from themis import benchreal as br

FAULT = br.FAULTS["bad-deploy"]


def agent(**kw):
    base = {"status": "completed", "proposed": {"action": "revert_release"}, "action": "revert_release", "outcome": "done"}
    return {**base, **kw}


@pytest.mark.parametrize(
    ("run", "recovered", "in_sync", "expected"),
    [
        (agent(), True, True, "fixed"),
        (agent(action="rollout_undo", proposed={"action": "rollout_undo"}), True, False, "fixed, Git out of sync"),
        (agent(), False, True, "right fix, no recovery"),
        (agent(action="restart_deployment", proposed={"action": "restart_deployment"}), True, True, "wrong action ran"),
        (
            agent(outcome="escalated", action="scale_deployment", proposed={"action": "scale_deployment"}),
            False,
            True,
            "wrong action stopped",
        ),  # fmt: skip
        (agent(outcome="denied", action="revert_release"), False, True, "right fix, stopped"),
        (agent(outcome="escalated", action="escalate", proposed={"action": "escalate"}), False, True, "handed to a person"),
        ({"status": "failed", "error": "boom"}, False, True, "agent failed"),
    ],
)
def test_every_way_a_run_can_end_has_a_plain_verdict(run, recovered, in_sync, expected):
    assert br.verdict(FAULT, run, recovered, in_sync) == expected


def test_the_page_is_the_first_shop_alert_that_fired_and_stock_cluster_alerts_never_page():
    alerts = [
        {"labels": {"alertname": "HighLatencyP95", "namespace": "shop", "service": "checkout"}, "state": "pending",
         "activeAt": "2026-11-26T10:00:00Z"},
        {"labels": {"alertname": "HighErrorRate", "namespace": "shop", "service": "checkout", "severity": "page"},
         "annotations": {"summary": "checkout 5xx error rate is 30%"}, "state": "firing", "activeAt": "2026-11-26T10:01:00Z"},
        {"labels": {"alertname": "HighErrorRate", "namespace": "shop", "service": "frontend"}, "state": "firing",
         "activeAt": "2026-11-26T10:00:30Z"},
    ]  # fmt: skip
    stock = {"labels": {"alertname": "InfoInhibitor", "namespace": "shop", "severity": "none"}, "state": "firing",
             "activeAt": "2026-11-26T09:00:00Z"}  # fmt: skip
    assert br.shop_pages([stock, *alerts]) == alerts[1:2]  # only the shop's own page-severity rules page anyone
    page = br.page_of(alerts)
    assert (page["alertname"], page["service"]) == ("HighErrorRate", "frontend")
    assert br.page_of(alerts[:1]) is None  # pending is not a page


def test_cost_uses_list_price_and_admits_what_it_does_not_know():
    assert br.cost("claude-haiku-5-5", 1_000_000, 100_000) == pytest.approx(0.15)
    assert br.cost("scripted-playbook", 10, 10) is None


def test_the_fleet_lets_the_agent_read_and_fix_but_scaling_is_high_risk():
    spec = br.fleet("claude-haiku-5-5")
    oncall = spec["agents"]["oncall"]
    assert "revert_release" in oncall["tools"] and "golden_signals" in oncall["tools"]
    assert "scale_deployment" not in oncall["tools"] and oncall["high_risk"] == ["scale_deployment"]
    assert spec["prices"] == {"claude-haiku-5-5": [0.10, 0.50]}


def trial(verdict, page_s=180.0, seconds=40.0, recovered_s=150.0, calls=9):
    a = agent(seconds=seconds, model_calls=calls, input_tokens=20_000, output_tokens=900, model="claude-haiku-5-5")
    return {"fault": "bad-deploy", "run": 1, "verdict": verdict, "page_s": page_s, "recovered_s": recovered_s,
            "agent": a, "cost": br.cost("claude-haiku-5-5", 20_000, 900), "page": None, "themis": {}, "in_sync": True}  # fmt: skip


def test_the_summary_counts_fixes_and_harm_and_takes_medians():
    trials = [trial("fixed", 170), trial("fixed, Git out of sync", 190), trial("wrong action ran", 400, recovered_s=None)]
    (row,) = br.summarise(trials)
    assert (row["runs"], row["fixed"], row["harmful"]) == (3, 2, 1)
    assert row["page_s"] == 190 and row["recovered_s"] == 150 and row["model_calls"] == 27
    assert row["cost"] == pytest.approx(3 * 0.002450)


def test_the_write_up_says_what_is_real_and_marks_a_plumbing_check():
    base = {"themis": "0.2.0", "date": "2026-11-26", "kubernetes": "v1.34.0", "machine": "Linux x86_64, 4 CPUs",
            "run_url": "https://example.invalid/run", "trials": [trial("fixed")]}  # fmt: skip
    base["summary"] = br.summarise(base["trials"])
    real = br.render({**base, "kind": "real", "model": "claude-haiku-5-5"})
    assert real.startswith("# Real-tools results") and "| bad-deploy | 1 | 1 of 1 | 0 |" in real
    assert "Scripted: the person on call" in real and "[The run](https://example.invalid/run)" in real
    check = br.render({**base, "kind": "plumbing check", "model": "scripted playbook (no model)"})
    assert check.startswith("# Real-tools plumbing check") and "none of it is a result" in check


# ------------------------------------------------------------------------------------------ a whole run, faked cluster

SIBLINGS = Path(os.getenv("THEMIS_SIBLINGS", "/nonexistent"))
PY = os.getenv("THEMIS_SIBLINGS_PYTHON", "")

KUBECTL = r'''#!/usr/bin/env python3
"""kubectl, as far as bench-real and the on-call agent use it. Deployment images live in a JSON file."""
import json, os, pathlib, re, sys

state_file = pathlib.Path(os.environ["FAKE_KUBE_STATE"])
state = json.loads(state_file.read_text()) if state_file.exists() else {"images": {}, "history": {}}
args = sys.argv[1:]
with open(str(state_file) + ".log", "a") as log:
    log.write(" ".join(args) + "\n")
if args[:2] == ["apply", "-k"]:
    for d in ("frontend", "checkout", "payments"):
        text = (pathlib.Path(args[2]) / "apps" / d / "deployment.yaml").read_text()
        image = re.search(r"image:\s*(\S+)", text).group(1)
        if state["images"].get(d) not in (None, image):
            state["history"].setdefault(d, []).append(state["images"][d])
        state["images"][d] = image
        print("deployment.apps/%s configured" % d)
elif "undo" in args:
    d = args[-1].split("/")[-1]
    state["images"][d] = state["history"][d].pop()
    print("deployment.apps/%s rolled back" % d)
elif args[:1] == ["version"]:
    print(json.dumps({"serverVersion": {"gitVersion": "v0-fake"}}))
elif "get" in args:
    sys.stdout.write(state["images"][args[args.index("deploy") + 1]])
else:
    print("ok")
state_file.write_text(json.dumps(state))
'''


class FakePrometheus(BaseHTTPRequestHandler):
    state_file: Path

    def do_GET(self):
        images = json.loads(self.state_file.read_text())["images"] if self.state_file.exists() else {}
        broken = images.get("checkout", "").endswith(":1.4.0")
        url = urlparse(self.path)
        if url.path == "/api/v1/alerts":
            alert = {"labels": {"alertname": "HighErrorRate", "namespace": "shop", "service": "frontend", "severity": "page"},
                     "annotations": {"summary": "frontend 5xx error rate is 31%"}, "state": "firing",
                     "activeAt": "2026-11-26T10:00:00Z"}  # fmt: skip
            data = {"alerts": [alert] if broken else []}
        else:
            q = parse_qs(url.query)["query"][0]
            hit = broken and ('service="checkout"' in q or 'service="frontend"' in q)
            data = {"result": [{"metric": {}, "value": [0, "0.31" if hit else "0"]}]}
        body = json.dumps({"status": "success", "data": data}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.mark.ecosystem
@pytest.mark.skipif(
    not PY or not (SIBLINGS / "argus-sre-agent").is_dir(), reason="set THEMIS_SIBLINGS and THEMIS_SIBLINGS_PYTHON"
)
def test_a_whole_bad_release_is_paged_worked_reverted_in_git_and_judged(tmp_path, monkeypatch):
    import subprocess

    gitops = tmp_path / "gitops"
    subprocess.run(["cp", "-r", str(SIBLINGS / "argus-sre-agent" / "gitops"), str(gitops)], check=True)
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "baseline"], ["tag", "baseline"]):  # fmt: skip
        subprocess.run(["git", *cmd], cwd=gitops, check=True)
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(KUBECTL.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "kube.json"
    FakePrometheus.state_file = state
    prom = ThreadingHTTPServer(("127.0.0.1", free_port()), FakePrometheus)
    threading.Thread(target=prom.serve_forever, daemon=True).start()
    fixture = SIBLINGS / "argus-sre-agent" / "evals" / "scenarios" / "01-bad-deploy.yaml"
    for k, v in {"FAKE_KUBE_STATE": str(state), "KUBECTL": str(kubectl), "ARGUS_BACKEND": "fixture",
                 "ARGUS_FIXTURE": str(fixture)}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(br, "TICK", 0.2)
    out = tmp_path / "out"
    cfg = {"faults": ["bad-deploy"], "runs": 1, "brain": "scripted", "model": "claude-haiku-5-5", "max_calls": 10,
           "max_total_calls": 50, "agent_python": PY, "agent_script": Path(__file__).parent.parent / "scripts" / "bench_real_agent.py",
           "gitops": str(gitops), "prometheus": f"http://127.0.0.1:{prom.server_port}", "port": free_port(), "out": str(out),
           "kubectl": str(kubectl), "screenshots": True}  # fmt: skip
    try:
        result = br.bench(cfg, log=lambda *a: None)
    finally:
        prom.shutdown()
    (t,) = result["trials"]
    assert t["verdict"] == "fixed", (out / "bad-deploy-1.agent.log").read_text()[-3000:]
    assert t["page"]["alertname"] == "HighErrorRate" and t["in_sync"]
    assert t["agent"]["action"] == "revert_release" and t["agent"]["model_calls"] == 4
    tools = [o["tool"] for o in t["agent"]["tools_used"]]
    assert {"list_firing_alerts", "rollout_history", "gitops_history"} <= set(tools)
    log = subprocess.run(["git", "log", "--format=%an %s"], cwd=gitops, capture_output=True, text=True).stdout
    assert log.splitlines()[0] == 'oncall-agent Revert "checkout: release 1.4.0"'
    assert json.loads(state.read_text())["images"]["checkout"] == "argus-demo/shop:1.3.2"
    assert t["themis"]["journey"]["outcome"] == "success" and t["themis"]["autonomy"] == "autonomous"
    assert result["kind"] == "plumbing check" and (out / "RESULTS-real.md").read_text().startswith("# Real-tools plumbing")
    # enough to draw the run afterwards: the error rate over time, when each thing happened, and Themis's own data
    assert t["timeline"] and {"t", "frontend", "checkout", "payments"} <= set(t["timeline"][0])
    ev = t["events"]
    assert ev["page"] <= ev["agent_start"] <= ev["agent_end"] <= ev["healthy"] <= ev["end"]
    assert (out / "themis.db").stat().st_size > 0
    if shutil.which("google-chrome") and importlib.util.find_spec("playwright"):
        assert result["screenshots"] == ["bench-real-agent.png", "bench-real-journey.png"]
        assert "](images/bench-real-agent.png)" in (out / "RESULTS-real.md").read_text()
